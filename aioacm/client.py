# coding: utf8
import hmac
import time
import json
import ssl
import base64
import asyncio
import hashlib
import logging

from functools import partial
from http import HTTPStatus
from asyncio import iscoroutinefunction
from urllib.parse import urlencode, unquote_plus
from urllib.error import HTTPError, URLError

from aiohttp import ClientSession, ClientError, ClientResponseError

from .commons import synchronized_with_attr, truncate
from .params import group_key, parse_key, is_valid
from .server import get_server_list
from .files import read_file, save_file, delete_file

LOGGER = logging.getLogger("aioacm")

DEBUG = False
VERSION = "0.3.1"

DEFAULT_GROUP_NAME = "DEFAULT_GROUP"
DEFAULT_NAMESPACE = ""

WORD_SEPARATOR = u'\x02'
LINE_SEPARATOR = u'\x01'

kms_available = False

try:
    from aliyunsdkcore.client import AcsClient
    from aliyunsdkkms.request.v20160120.DecryptRequest import DecryptRequest
    from aliyunsdkkms.request.v20160120.EncryptRequest import EncryptRequest

    kms_available = True
except ImportError:
    LOGGER.info("Aliyun KMS SDK is not installed")

ENCRYPTED_DATA_ID_PREFIX = "cipher-"

DEFAULTS = {
    "APP_NAME": "ACM-SDK-Python",
    "TIMEOUT": 3,  # in seconds
    "PULLING_TIMEOUT": 30,  # in seconds
    "PULLING_CONFIG_SIZE": 3000,
    "CALLBACK_THREAD_NUM": 10,
    "FAILOVER_BASE": "acm-data/data",
    "SNAPSHOT_BASE": "acm-data/snapshot",
    "KMS_ENABLED": False,
    "REGION_ID": "",
    "KEY_ID": "",
}

OPTIONS = {
    "default_timeout",
    "tls_enabled",
    "auth_enabled",
    "cai_enabled",
    "pulling_timeout",
    "pulling_config_size",
    "callback_thread_num",
    "failover_base",
    "snapshot_base",
    "app_name",
    "kms_enabled",
    "region_id",
    "kms_ak",
    "kms_secret",
    "key_id"
}

_FUTURES = []


class ACMException(Exception):
    pass


class ACMRequestException(ACMException):
    pass


def process_common_params(data_id, group):
    if not group or not group.strip():
        group = DEFAULT_GROUP_NAME
    else:
        group = group.strip()

    if not data_id or not is_valid(data_id):
        raise ACMException("Invalid dataId.")

    if not is_valid(group):
        raise ACMException("Invalid group.")
    return data_id, group


def parse_pulling_result(result):
    if not result:
        return list()
    ret = list()
    for i in unquote_plus(result).split(LINE_SEPARATOR):
        if not i.strip():
            continue
        sp = i.split(WORD_SEPARATOR)
        if len(sp) < 3:
            sp.append("")
        ret.append(sp)
    return ret


def is_encrypted(data_id):
    return data_id.startswith(ENCRYPTED_DATA_ID_PREFIX)


class WatcherWrap:
    def __init__(self, key, callback):
        self.callback = callback
        self.last_md5 = None
        self.watch_key = key


class CacheData:
    def __init__(self, key, client):
        self.key = key
        local_value = read_file(client.failover_base, key) or \
            read_file(client.snapshot_base, key)
        self.content = local_value
        if isinstance(local_value, bytes):
            src = local_value.decode("utf8")
        else:
            src = local_value
        self.md5 = hashlib.md5(src.encode("GBK")).hexdigest() if src else None
        self.is_init = True
        if not self.md5:
            LOGGER.getChild('init-cache').debug(
                "cache for %s does not have local value",
                key
            )


class ACMClient:
    """Client for ACM

    available API:
    * get
    * add_watcher
    * remove_watcher
    """
    debug = False

    @staticmethod
    def set_debugging():
        if not ACMClient.debug:
            handler = logging.StreamHandler()
            handler.setFormatter(
                logging.Formatter(
                    "%(asctime)s %(levelname)s %(name)s:%(message)s"
                )
            )
            LOGGER.addHandler(handler)
            LOGGER.setLevel(logging.DEBUG)
            ACMClient.debug = True

    def __init__(self, endpoint, namespace=None, ak=None, sk=None):
        self.endpoint = endpoint
        self.namespace = namespace or DEFAULT_NAMESPACE or ""
        self.ak = ak
        self.sk = sk

        self.server_list = None
        self.server_list_lock = asyncio.Lock()
        self.current_server = None
        self.server_offset = 0
        self.server_refresh_running = False

        self.watcher_mapping = dict()
        self.pulling_lock = asyncio.Lock()
        self.puller_mapping = None
        self.notify_queue = None
        self.callback_tread_pool = None

        self.default_timeout = DEFAULTS["TIMEOUT"]
        self.tls_enabled = False
        self.auth_enabled = self.ak and self.sk
        self.cai_enabled = True
        self.pulling_timeout = DEFAULTS["PULLING_TIMEOUT"]
        self.pulling_config_size = DEFAULTS["PULLING_CONFIG_SIZE"]
        self.callback_tread_num = DEFAULTS["CALLBACK_THREAD_NUM"]
        self.failover_base = DEFAULTS["FAILOVER_BASE"]
        self.snapshot_base = DEFAULTS["SNAPSHOT_BASE"]
        self.app_name = DEFAULTS["APP_NAME"]
        self.kms_enabled = DEFAULTS["KMS_ENABLED"]
        self.region_id = DEFAULTS["REGION_ID"]
        self.key_id = DEFAULTS["KEY_ID"]
        self.kms_ak = self.ak
        self.kms_secret = self.sk
        self.kms_client = None
        self.logger = LOGGER
        self.logger.getChild('client-init').info(
            "endpoint:%s, tenant:%s",
            endpoint,
            namespace
        )

    def set_options(self, **kwargs):
        logger = self.logger.getChild('set_options')
        for k, v in kwargs.items():
            if k not in OPTIONS:
                logger.warning("unknown option:%s, ignored" % k)
                continue

            if k == "kms_enabled" and v and not kms_available:
                logger.warning("kms can not be turned on with no KMS SDK installed")
                continue

            logger.debug("key:%s, value:%s" % (k, v))
            setattr(self, k, v)

    async def _refresh_server_list(self):
        logger = self.logger.getChild('refresh-server')
        async with self.server_list_lock:
            if self.server_refresh_running:
                logger.warning("task is running, aborting")
                return
            self.server_refresh_running = True

        while True:
            try:
                await asyncio.sleep(30)
                logger.debug("try to refresh server list")
                server_list = await get_server_list(
                    self.endpoint,
                    443 if self.tls_enabled else 8080,
                    self.cai_enabled
                )
                logger.debug(
                    "server_num:%s server_list:%s",
                    len(server_list),
                    server_list
                )
                if not server_list:
                    logger.error(
                        "empty server_list get from %s, "
                        "do not refresh",
                        self.endpoint
                    )
                    continue
                async with self.server_list_lock:
                    self.server_list = server_list
                    self.server_offset = 0
                    if self.current_server not in server_list:
                        logger.warning(
                            "%s is not effective, change one",
                            str(self.current_server)
                        )
                        self.current_server = server_list[self.server_offset]
            except Exception as e:
                logger.exception("exception %s occur", str(e))

    async def change_server(self):
        async with self.server_list_lock:
            self.server_offset = (
                (self.server_offset + 1) % len(self.server_list)
            )
            self.current_server = self.server_list[self.server_offset]

    async def get_server(self):
        logger = self.logger.getChild('get-server')
        if self.server_list is None:
            async with self.server_list_lock:
                logger.info(
                    "server list is null, try to initialize"
                )
                server_list = await get_server_list(
                    self.endpoint,
                    443 if self.tls_enabled else 8080,
                    self.cai_enabled
                )
                if not server_list:
                    logger.error(
                        "empty server_list get from %s",
                        self.endpoint
                    )
                    return None
                self.server_list = server_list
                self.current_server = self.server_list[self.server_offset]
                logger.info(
                    "server_num:%s server_list:%s",
                    len(self.server_list),
                    self.server_list
                )

            if self.cai_enabled:
                future = asyncio.ensure_future(
                        self._refresh_server_list()
                    )
                future.add_done_callback(partial(self.log_and_rerun_on_failure, self._refresh_server_list))
                # close job than run in backgroud.
                _FUTURES.append(future)

        logger.info("use server:%s" % str(self.current_server))
        return self.current_server

    async def remove(self, data_id, group, timeout=None):
        """ Remove one data item from ACM.

        :param data_id: dataId.
        :param group: group, use "DEFAULT_GROUP" if no group specified.
        :param timeout: timeout for requesting server in seconds.
        :return: True if success or an exception will be raised.
        """
        logger = self.logger.getChild("remove")
        data_id, group = process_common_params(data_id, group)
        logger.info(
            "data_id:%s, group:%s, namespace:%s, timeout:%s" % (data_id, group, self.namespace, timeout))

        params = {
            "dataId": data_id,
            "group": group,
        }
        if self.namespace:
            params["tenant"] = self.namespace

        try:
            resp = await self._do_sync_req("/diamond-server/datum.do?method=deleteAllDatums", None, None, params,
                                           'POST', timeout or self.default_timeout)
            logger.info("success to remove group:%s, data_id:%s, server response:%s" % (
                group, data_id, resp))
        except ClientResponseError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                logger.error(
                    "no right for namespace:%s, group:%s, data_id:%s" % (self.namespace, group, data_id))
                raise ACMException("Insufficient privilege.")
            else:
                logger.error("error code [:%s] for namespace:%s, group:%s, data_id:%s" % (
                    e.code, self.namespace, group, data_id))
                raise ACMException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("exception %s occur" % str(e))
            raise
        cache_key = group_key(data_id, group, self.namespace)
        delete_file(self.snapshot_base, cache_key)
        return True

    async def publish(self, data_id, group, content, timeout=None):
        """ Publish one data item to ACM.

        If the data key is not exist, create one first.
        If the data key is exist, update to the content specified.
        Content can not be set to None, if there is need to delete config item, use function **remove** instead.

        :param data_id: dataId.
        :param group: group, use "DEFAULT_GROUP" if no group specified.
        :param content: content of the data item.
        :param timeout: timeout for requesting server in seconds.
        :return: True if success or an exception will be raised.
        """
        logger = self.logger.getChild('publish')
        if content is None:
            raise ACMException("Can not publish none content, use remove instead.")

        data_id, group = process_common_params(data_id, group)
        if isinstance(content, bytes):
            content = content.decode("utf8")

        if is_encrypted(data_id) and self.kms_enabled:
            content = self.encrypt(content)

        logger.info("data_id:%s, group:%s, namespace:%s, content:%s, timeout:%s" % (
            data_id, group, self.namespace, truncate(content), timeout))
        params = {
            "dataId": data_id,
            "group": group,
            "content": content,
        }
        if self.namespace:
            params["tenant"] = self.namespace
        try:
            resp = await self._do_sync_req("/diamond-server/basestone.do?method=syncUpdateAll", None, None, params,
                                           'POST', timeout or self.default_timeout)
            logger.info("success to publish content, group:%s, data_id:%s, server response:%s" % (
                group, data_id, resp))
            return True
        except ClientResponseError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                logger.error(
                    "no right for namespace:%s, group:%s, data_id:%s" % (self.namespace, group, data_id))
                raise ACMException("Insufficient privilege.")
            else:
                logger.error("error code [:%s] for namespace:%s, group:%s, data_id:%s" % (
                    e.code, self.namespace, group, data_id))
                raise ACMException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("exception %s occur" % str(e))
            raise

    async def get(self, data_id, group, timeout=None, no_snapshot=False):
        content = await self.get_raw(data_id, group, timeout, no_snapshot)
        if content and is_encrypted(data_id) and self.kms_enabled:
            return self.decrypt(content)
        return content

    async def get_raw(self, data_id, group, timeout=None, no_snapshot=False):
        """Get value of one config item.

        query priority:
        1.  get from local failover dir(default: "{cwd}/acm/data")
            failover dir can be manually copied from snapshot
            dir(default: "{cwd}/acm/snapshot") in advance
            this helps to suppress the effect of known server failure

        2.  get from one server until value is got or all servers tried
            content will be save to snapshot dir

        3.  get from snapshot dir

        :param data_id: dataId
        :param group: group, use "DEFAULT_GROUP" if no group specified
        :param timeout: timeout for requesting server in seconds
        :return: value
        """
        logger = self.logger.getChild('get-config')

        data_id, group = process_common_params(data_id, group)
        logger.info(
            "data_id:%s, group:%s, namespace:%s, timeout:%s",
            data_id,
            group,
            self.namespace,
            timeout
        )

        params = {
            "dataId": data_id,
            "group": group,
        }
        if self.namespace:
            params["tenant"] = self.namespace

        cache_key = group_key(data_id, group, self.namespace)
        # get from failover
        content = read_file(self.failover_base, cache_key)
        if content is None:
            logger.debug(
                "failover config is not exist for %s, "
                "try to get from server",
                cache_key
            )
        else:
            logger.debug(
                "get %s from failover directory, content is %s",
                cache_key,
                truncate(content)
            )
            return content

        try:
            content = await self._do_sync_req(
                "/diamond-server/config.co",
                None,
                params,
                None,
                'GET',
                timeout or self.default_timeout
            )
        except ClientResponseError as e:
            if e.code == HTTPStatus.NOT_FOUND:
                logger.warning(
                    "config not found for data_id:%s, group:%s, "
                    "namespace:%s, try to delete snapshot",
                    data_id,
                    group,
                    self.namespace
                )
                delete_file(self.snapshot_base, cache_key)
                return None
            elif e.code == HTTPStatus.CONFLICT:
                logger.error(
                    "config being modified concurrently for "
                    "data_id:%s, group:%s, namespace:%s",
                    data_id,
                    group,
                    self.namespace
                )
            elif e.code == HTTPStatus.FORBIDDEN:
                logger.error(
                    "no right for data_id:%s, group:%s, "
                    "namespace:%s",
                    data_id,
                    group,
                    self.namespace
                )
                raise ACMException("Insufficient privilege.")
            else:
                logger.error(
                    "error code [:%s] for data_id:%s, group:%s, "
                    "namespace:%s",
                    e.code,
                    data_id,
                    group,
                    self.namespace
                )
                if no_snapshot:
                    raise
        except ACMException as e:
            logger.error("acm exception: %s" % str(e))
        except Exception as e:
            logger.exception("exception %s occur" % str(e))
            if no_snapshot:
                raise

        if no_snapshot:
            return content

        if content is not None:
            logger.info(
                "content from server:%s, data_id:%s, group:%s, "
                "namespace:%s, try to save snapshot",
                truncate(content),
                data_id,
                group,
                self.namespace
            )
            try:
                save_file(self.snapshot_base, cache_key, content)
            except Exception as e:
                logger.error(
                    "save snapshot failed for %s, data_id:%s, "
                    "group:%s, namespace:%s",
                    data_id,
                    group,
                    self.namespace,
                    str(e)
                )
            return content

        logger.error(
            "get config from server failed, try snapshot, "
            "data_id:%s, group:%s, namespace:%s",
            data_id,
            group,
            self.namespace
        )
        content = read_file(self.snapshot_base, cache_key)
        if content is None:
            logger.warning(
                "snapshot is not exist for %s.",
                cache_key
            )
        else:
            logger.debug(
                "get %s from snapshot directory, content is %s",
                cache_key,
                truncate(content)
            )
            return content

    async def list(self, page=1, size=200):
        """ Get config items of current namespace with content included.

        Data is directly from acm server.

        :param page: which page to query, starts from 1.
        :param size: page size.
        :return:
        """
        logger = self.logger.getChild("list")
        logger.info("try to list namespace:%s" % self.namespace)

        params = {
            "pageNo": page,
            "pageSize": size,
            "method": "getAllConfigByTenant",
        }

        if self.namespace:
            params["tenant"] = self.namespace

        try:
            d = await self._do_sync_req("/diamond-server/basestone.do", None, params, None, 'GET', self.default_timeout)
            return json.loads(d)
        except ClientResponseError as e:
            if e.code == HTTPStatus.FORBIDDEN:
                logger.error("no right for namespace:%s" % self.namespace)
                raise ACMException("Insufficient privilege.")
            else:
                logger.error("[list] error code [%s] for namespace:%s" % (e.code, self.namespace))
                raise ACMException("Request Error, code is %s" % e.code)
        except Exception as e:
            logger.exception("exception %s occur" % str(e))
            raise

    async def list_all(self, group=None, prefix=None):
        """ Get all config items of current namespace, with content included.

        Warning: If there are lots of config in namespace, this function may cost some time.

        :param group: only dataIds with group match shall be returned.
        :param prefix: only dataIds startswith prefix shall be returned **it's case sensitive**.
        :return:
        """
        logger = self.logger.getChild("list-all")
        logger.info("namespace:%s, group:%s, prefix:%s" % (self.namespace, group, prefix))

        def matching(ori):
            return (group is None or ori["group"] == group) and (prefix is None or ori["dataId"].startswith(prefix))

        result = await self.list(1, 200)
        if not result:
            logger.warning("can not get config items of %s" % self.namespace)
            return list()

        ret_list = [{"dataId": i["dataId"], "group": i["group"]} for i in result["pageItems"] if matching(i)]
        pages = result["pagesAvailable"]
        logger.debug("%s items got from acm server" % result["totalCount"])

        for i in range(2, pages + 1):
            result = await self.list(i, 200)
            ret_list += [{"dataId": j["dataId"], "group": j["group"]} for j in result["pageItems"] if matching(j)]
        logger.debug("%s items returned" % len(ret_list))
        return ret_list

    @synchronized_with_attr("pulling_lock")
    def add_watcher(self, data_id, group, cb):
        self.add_watchers(data_id, group, [cb])

    @synchronized_with_attr("pulling_lock")
    def add_watchers(self, data_id, group, cb_list):
        """Add watchers to specified item.

        1.  callback is invoked from current process concurrently by
            thread pool
        2.  callback is invoked at once if the item exists
        3.  callback is invoked if changes or deletion detected on the item

        :param data_id: data_id
        :param group: group, use "DEFAULT_GROUP" if no group specified
        :param cb_list: callback functions
        :return:
        """
        logger = self.logger.getChild("add-watcher")
        if not cb_list:
            raise ACMException("A callback function is needed.")
        data_id, group = process_common_params(data_id, group)
        logger.info(
            "data_id:%s, group:%s, namespace:%s",
            data_id,
            group,
            self.namespace
        )
        cache_key = group_key(data_id, group, self.namespace)
        wl = self.watcher_mapping.get(cache_key)
        if not wl:
            wl = list()
            self.watcher_mapping[cache_key] = wl
        for cb in cb_list:
            wl.append(WatcherWrap(cache_key, cb))
            if hasattr(cb, '__name__'):
                cb_name = cb.__name__
            elif hasattr(cb, 'func'):
                cb_name = cb.func.__name__
            else:
                cb_name = str(cb)
            logger.info(
                "watcher has been added for key:%s, "
                "new callback is:%s, callback number is:%s",
                cache_key,
                cb_name,
                len(wl)
            )

        if self.puller_mapping is None:
            logger.debug("pulling should be initialized")
            self._int_pulling()

        def callback():
            if cache_key in self.puller_mapping:
                logger.debug(
                    "key:%s is already in pulling",
                    cache_key
                )
                return

            for key, puller_info in self.puller_mapping.items():
                if len(puller_info[1]) < self.pulling_config_size:
                    logger.debug(
                        "puller:%s is available, add key:%s",
                        puller_info[0],
                        cache_key
                    )
                    puller_info[1].append(cache_key)
                    self.puller_mapping[cache_key] = puller_info
                    break
            else:
                logger.debug(
                    "no puller available, "
                    "new one and add key:%s",
                    cache_key
                )
                key_list = []
                key_list.append(cache_key)
                puller = asyncio.ensure_future(
                    self._do_pulling(key_list, self.notify_queue)
                )
                self.puller_mapping[cache_key] = (puller, key_list)
                puller.add_done_callback(
                    partial(
                        self.log_and_update_puller_on_failure,
                        self._do_pulling,
                        key_list, self.notify_queue,
                        cache_key=cache_key
                    )
                )

        asyncio.get_event_loop().call_soon(callback)

    def log_and_update_puller_on_failure(self, coro, *args, **kwargs):
        logger = self.logger.getChild("callback")
        future = args[-1]
        exc = future.exception()
        if exc:
            logger.error('Exception happened on future', exc_info=exc)
            args = args[:-1]
            cache_key = kwargs.pop('cache_key')
            new_future = asyncio.ensure_future(coro(*args, **kwargs))
            self.puller_mapping[cache_key] = (new_future, self.puller_mapping[cache_key][1])
            new_future.add_done_callback(
                partial(
                    self.log_and_rerun_on_failure,
                    coro, *args, **kwargs, cache_key=cache_key)
            )

    @synchronized_with_attr("pulling_lock")
    def remove_watcher(self, data_id, group, cb, remove_all=False):
        """Remove watcher from specified key

        :param data_id: data_id
        :param group: group, use "DEFAULT_GROUP" if no group specified
        :param cb: callback function
        :param remove_all: weather to remove all occurrence of the callback
                            or just once
        :return:
        """
        logger = self.logger.getChild("remove-watcher")
        if not cb:
            raise ACMException("A callback function is needed.")
        data_id, group = process_common_params(data_id, group)
        if not self.puller_mapping:
            logger.warning("[remove-watcher] watcher is never started.")
            return
        cache_key = group_key(data_id, group, self.namespace)
        wl = self.watcher_mapping.get(cache_key)
        if not wl:
            logger.warning(
                "[remove-watcher] there is no watcher on key:%s",
                cache_key
            )
            return

        wrap_to_remove = list()
        for i in wl:
            if i.callback == cb:
                wrap_to_remove.append(i)
                if not remove_all:
                    break

        for i in wrap_to_remove:
            wl.remove(i)

        if hasattr(cb, '__name__'):
            cb_name = cb.__name__
        elif hasattr(cb, 'func'):
            cb_name = cb.func.__name__
        else:
            cb_name = str(cb)

        logger.info(
            "[remove-watcher] %s is removed from %s, remove all:%s",
            cb_name,
            cache_key,
            remove_all
        )
        if not wl:
            logger.debug(
                "[remove-watcher] there is no watcher for:%s, "
                "kick out from pulling",
                cache_key
            )
            self.watcher_mapping.pop(cache_key)
            puller_info = self.puller_mapping[cache_key]
            puller_info[1].remove(cache_key)
            if not puller_info[1]:
                logger.debug(
                    "[remove-watcher] there is no pulling keys for puller:%s, "
                    "stop it",
                    puller_info[0]
                )
                self.puller_mapping.pop(cache_key)
                puller_info[0].cancel()

    async def _do_sync_req(self, url: str, headers: dict = None,
                           params: dict = None, data: str = None,
                           method: str = 'get', timeout: int = None):
        logger = self.logger.getChild("do-sync-req")

        # url = "?".join([url, urlencode(params)]) if params else url
        all_headers = self._get_common_headers(params, data)
        if headers:
            all_headers.update(headers)
        logger.debug(
            "url:%s, headers:%s, params:%s, data:%s, timeout:%s",
            url,
            all_headers,
            params,
            data,
            timeout
        )
        tries = 0
        while True:
            try:
                server_info = await self.get_server()
                if not server_info:
                    logger.error("can not get one server.")
                    raise ACMException("Server is not available.")
                address, port, is_ip_address = server_info
                server = ":".join([address, str(port)])
                # if tls is enabled and server address is in ip,
                # turn off verification

                server_url = "%s://%s%s" % (
                    "https" if self.tls_enabled else "http",
                    server,
                    url
                )
                async with ClientSession() as request:
                    if method.upper() == 'POST':
                        if data and not isinstance(data, bytes):
                            data = urlencode(data, encoding='GBK').encode()
                        request_ctx = request.post(
                            server_url,
                            headers=all_headers,
                            params=params,
                            data=data,
                            timeout=timeout
                        )
                    else:
                        request_ctx = request.get(
                            server_url,
                            headers=all_headers,
                            params=params,
                            timeout=timeout
                        )
                    async with request_ctx as resp:
                        resp.raise_for_status()
                        text = await resp.text()

                        if resp.status > 300:
                            raise HTTPError(server_url, resp.status,
                                            resp.reason, all_headers, None)

                    logger.debug(
                        "info from server:%s",
                        server
                    )
                    return text
            except HTTPError as e:
                if e.code in [
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    HTTPStatus.BAD_GATEWAY,
                    HTTPStatus.SERVICE_UNAVAILABLE
                ]:
                    logger.warning(
                        "server:%s is not available for "
                        "reason:%s",
                        server,
                        e.msg
                    )
                else:
                    raise
            except asyncio.TimeoutError:
                logger.warning("%s request timeout", server)
            except ClientError as exc:
                logger.warning(
                    "%s request error. %s",
                    server,
                    exc
                )
            except URLError as e:
                logger.warning(
                    "%s connection error:%s",
                    server,
                    e.reason
                )

            tries += 1
            if tries >= len(self.server_list):
                logger.error(
                    "%s maybe down, no server is currently "
                    "available",
                    server
                )
                raise ACMRequestException("All server are not available")
            await self.change_server()
            logger.warning("%s maybe down, skip to next", server)

    async def _do_pulling(self, cache_list: list, queue: asyncio.Queue):
        logger = self.logger.getChild("do-pulling")
        cache_pool = dict()
        for cache_key in cache_list:
            cache_pool[cache_key] = CacheData(cache_key, self)

        while cache_list:
            unused_keys = set(cache_pool.keys())
            contains_init_key = False
            probe_update_string = ""
            for cache_key in cache_list:
                cache_data = cache_pool.get(cache_key)
                if not cache_data:
                    logger.debug("new key added: %s" % cache_key)
                    cache_data = CacheData(cache_key, self)
                    cache_pool[cache_key] = cache_data
                if cache_data.is_init:
                    contains_init_key = True
                data_id, group, namespace = parse_key(cache_key)
                probe_update_string += WORD_SEPARATOR.join([
                    data_id,
                    group,
                    cache_data.md5 or "",
                    self.namespace
                ])
                probe_update_string += LINE_SEPARATOR
                try:
                    unused_keys.remove(cache_key)
                except KeyError:
                    pass
            for k in unused_keys:
                logger.debug(
                    "%s is no longer watched, remove from cache",
                    k
                )
                cache_pool.pop(k)

            logger.debug(
                "try to detected change from server probe "
                "string is %s",
                truncate(probe_update_string)
            )
            headers = {
                "longPullingTimeout": str(int(self.pulling_timeout * 1000))
            }
            if contains_init_key:
                headers["longPullingNoHangUp"] = "true"

            data = {"Probe-Modify-Request": probe_update_string}

            changed_keys = list()
            try:
                resp = await self._do_sync_req(
                    "/diamond-server/config.co",
                    headers,
                    None,
                    data,
                    'POST',
                    self.pulling_timeout + 10
                )
                changed_keys = [
                    group_key(*i)
                    for i in parse_pulling_result(resp)
                ]
                logger.debug(
                    "following keys are changed from server %s",
                    truncate(str(changed_keys))
                )
            except ACMException as e:
                logger.error("acm exception: %s" % str(e))
            except Exception as e:
                logger.error(
                    "exception %s occur, return empty list",
                    str(e)
                )

            for cache_key, cache_data in cache_pool.items():
                cache_data.is_init = False
                if cache_key in changed_keys:
                    data_id, group, namespace = parse_key(cache_key)
                    content = await self.get(data_id, group)
                    if content is not None:
                        md5 = hashlib.md5(content.encode("GBK")).hexdigest()
                    else:
                        md5 = None
                    cache_data.md5 = md5
                    cache_data.content = content
                await queue.put(
                    (cache_key, cache_data.content, cache_data.md5)
                )

    @synchronized_with_attr("pulling_lock")
    def _int_pulling(self):
        logger = self.logger.getChild("init-pulling")
        if self.puller_mapping is not None:
            logger.info("puller is already initialized")
            return
        self.puller_mapping = dict()
        self.notify_queue = asyncio.Queue()
        self.callbacks = []
        future = asyncio.ensure_future(self._process_polling_result())
        future.add_done_callback(partial(self.log_and_rerun_on_failure, self._process_polling_result))
        logger.info("init completed")

    async def _process_polling_result(self):
        logger = self.logger.getChild("process-polling-result")
        while True:
            cache_key, content, md5 = await self.notify_queue.get()
            logger.debug(
                "receive an event:%s",
                cache_key
            )
            wl = self.watcher_mapping.get(cache_key)
            if not wl:
                logger.warning(
                    "no watcher on %s, ignored",
                    cache_key
                )
                continue

            data_id, group, namespace = parse_key(cache_key)
            params = {
                "data_id": data_id,
                "group": group,
                "namespace": namespace,
                "content": content
            }
            for watcher in wl:
                if not watcher.last_md5 == md5:
                    cb = watcher.callback
                    if hasattr(cb, '__name__'):
                        cb_name = cb.__name__
                    elif hasattr(cb, 'func'):
                        cb_name = cb.func.__name__
                    else:
                        cb_name = str(cb)
                    logger.debug(
                        "md5 changed since last "
                        "call, calling %s",
                        cb_name
                    )
                    try:
                        if iscoroutinefunction(watcher.callback):
                            await watcher.callback(params)
                        else:
                            watcher.callback(params)
                    except Exception as e:
                        logger.exception(
                            "exception %s occur "
                            "while calling %s ",
                            str(e),
                            cb_name
                        )
                    watcher.last_md5 = md5

    def _get_common_headers(self, params, data):
        headers = {
            "Diamond-Client-AppName": self.app_name,
            "Client-Version": VERSION,
            "exConfigInfo": "true",
        }
        if data:
            headers["Content-Type"] = "application/x-www-form-urlencoded; charset=GBK"

        if self.auth_enabled:
            ts = str(int(time.time() * 1000))
            headers.update({
                "Spas-AccessKey": self.ak,
                "timeStamp": ts,
            })
            sign_str = ""
            # in case tenant or group is null
            if not params and not data:
                return headers

            tenant = (params and params.get("tenant")) or (data and data.get("tenant"))
            group = (params and params.get("group")) or (data and data.get("group"))

            if tenant:
                sign_str = tenant + "+"
            if group:
                sign_str = sign_str + group + "+"
            if sign_str:
                sign_str += ts
                headers["Spas-Signature"] = (
                    base64.encodebytes(
                        hmac.new(
                            self.sk.encode(),
                            sign_str.encode(),
                            digestmod=hashlib.sha1
                        )
                        .digest()
                    )
                    .decode()
                    .strip()
                )
        return headers

    def _prepare_kms(self):
        if not (self.region_id and self.kms_ak and self.kms_secret):
            return False
        if not self.kms_client:
            self.kms_client = AcsClient(ak=self.kms_ak, secret=self.kms_secret, region_id=self.region_id)
        return True

    def encrypt(self, plain_txt):
        if not self._prepare_kms():
            return plain_txt
        ssl._create_default_https_context = ssl._create_unverified_context
        req = EncryptRequest()
        req.set_KeyId(self.key_id)
        req.set_Plaintext(plain_txt if type(plain_txt) == bytes else plain_txt.encode("utf8"))
        resp = json.loads(self.kms_client.do_action_with_exception(req))
        return resp["CiphertextBlob"]

    def decrypt(self, cipher_blob):
        if not self._prepare_kms():
            return cipher_blob
        ssl._create_default_https_context = ssl._create_unverified_context
        req = DecryptRequest()
        req.set_CiphertextBlob(cipher_blob)
        resp = json.loads(self.kms_client.do_action_with_exception(req))
        return resp["Plaintext"]

    def log_and_rerun_on_failure(self, coro, *args, **kwargs):
        logger = self.logger.getChild('callback')
        future = args[-1]
        exc = future.exception()
        if exc:
            logger.error('Exception happened on future', exc_info=exc)
            args = args[:-1]
            new_future = asyncio.ensure_future(coro(*args, **kwargs))
            new_future.add_done_callback(
                partial(
                    self.log_and_rerun_on_failure,
                    coro, *args, **kwargs)
            )


if DEBUG:
    ACMClient.set_debugging()
