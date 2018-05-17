# -*- coding: utf8 -*-

import unittest
import shutil
import asyncio

import pytest

import aioacm
from aioacm import files

ENDPOINT = "acm.aliyun.com:8080"
NAMESPACE = "81597****2b55bac3"
AK = "4c796a4****ba83a296b489"
SK = "UjLe****faOk1E="
KMS_AK = "LT****yI"
KMS_SECRET = "xzhB****gb01"
KEY_ID = "ed0****67be"
REGION_ID = "cn-shanghai"


pytestmark = pytest.mark.asyncio


async def test_get_server():
    aioacm.ACMClient.set_debugging()
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    assert type(await c.get_server()) == tuple


async def test_get_server_err():
    c2 = aioacm.ACMClient("100.100.84.215:8080")
    assert await c2.get_server() is None
    c3 = aioacm.ACMClient("10.101.84.215:8081")
    assert await c3.get_server() is None


async def test_get_server_no_cai():
    c = aioacm.ACMClient("11.162.248.130:8080")
    c.set_options(cai_enabled=False)
    data_id = "com.alibaba"
    group = ""
    assert (await c.get(data_id, group)) is None


async def test_get_key():
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    data_id = "com.alibaba.cloud.acm:sample-app.properties"
    group = "sandbox"
    assert (await c.get(data_id, group)) is not None


async def test_no_auth():
    c = aioacm.ACMClient("jmenv.tbsite.net:8080")
    data_id = "com.alibaba"
    group = ""
    assert await c.get(data_id, group) is None


async def test_tls():
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    c.set_options(tls_enabled=True)
    data_id = "com.alibaba.cloud.acm:sample-app.properties"
    group = "sandbox"
    assert await c.get(data_id, group) is not None


async def test_server_failover():
    aioacm.ACMClient.set_debugging()
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    c.server_list = [("1.100.84.215", 8080, True), ("139.196.135.144", 8080, True)]
    c.current_server = ("1.100.84.215", 8080, True)
    data_id = "com.alibaba.cloud.acm:sample-app.properties"
    group = "sandbox"
    assert await c.get(data_id, group) is not None


async def test_server_failover_comp():
    aioacm.ACMClient.set_debugging()
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    await c.get_server()
    c.server_list = [("1.100.84.215", 8080, True), ("100.196.135.144", 8080, True)]
    c.current_server = ("1.100.84.215", 8080, True)
    data_id = "com.alibaba.cloud.acm:sample-app.properties"
    group = "sandbox"
    shutil.rmtree(c.snapshot_base, True)
    assert await c.get(data_id, group) is None
    await asyncio.sleep(31)
    shutil.rmtree(c.snapshot_base, True)
    assert await c.get(data_id, group) is not None


async def test_fake_watcher():
    data_id = "com.alibaba"
    group = "tsing"

    class Share:
        content = None
        count = 0

    cache_key = "+".join([data_id, group, ""])

    def test_cb(args):
        print(args)
        Share.count += 1
        Share.content = args["content"]

    c = aioacm.ACMClient(ENDPOINT)
    c.add_watcher(data_id, group, test_cb)
    c.add_watcher(data_id, group, test_cb)
    c.add_watcher(data_id, group, test_cb)
    await asyncio.sleep(1)
    await c.notify_queue.put((cache_key, "xxx", "md51"))
    await asyncio.sleep(2)
    assert Share.content == "xxx"
    assert Share.count == 3
    c.remove_watcher(data_id, group, test_cb)
    print('remove')
    Share.count = 0
    await c.notify_queue.put((cache_key, "yyy", "md52"))
    await asyncio.sleep(2)
    assert Share.content == "yyy"
    assert Share.count == 2
    c.remove_watcher(data_id, group, test_cb, True)
    Share.count = 0
    await c.notify_queue.put((cache_key, "not effective, no watchers", "md53"))
    await asyncio.sleep(2)
    assert Share.content == "yyy"
    assert Share.count == 0
    Share.count = 0
    c.add_watcher(data_id, group, test_cb)
    await asyncio.sleep(1)
    await c.notify_queue.put((cache_key, "zzz", "md54"))
    await asyncio.sleep(2)
    assert Share.content == "zzz"
    assert Share.count == 1
    Share.count = 0
    await c.notify_queue.put((cache_key, "not effective, md5 no changes", "md54"))
    await asyncio.sleep(2)
    assert Share.content == "zzz"
    assert Share.count == 0
    c.remove_watcher(data_id, group, test_cb)


async def test_long_pulling():
    aioacm.ACMClient.set_debugging()
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)

    class Share:
        content = None

    def cb(x):
        Share.content = x["content"]
        print(Share.content)
    # test common
    data_id = "com.alibaba.cloud.acm:sample-app.properties"
    group = "sandbox"
    c.add_watcher(data_id, group, cb)
    await asyncio.sleep(10)
    assert(Share.content, None)
    c.remove_watcher(data_id, group, cb)


async def test_get_from_failover():
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    data_id = "com.alibaba.cloud.acm:sample-app.properties"
    group = "sandbox"
    key = "+".join([data_id, group, NAMESPACE])
    files.save_file(c.failover_base, key, "xxx")
    assert await c.get(data_id, group) == "xxx"
    shutil.rmtree(c.failover_base)


async def test_get_from_snapshot():
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    c.server_list = [("1.100.84.215", 8080, True)]
    data_id = "com.alibaba.cloud.acm:sample-app.properties"
    group = "sandbox"
    key = "+".join([data_id, group, NAMESPACE])
    files.save_file(c.snapshot_base, key, "yyy")
    assert await c.get(data_id, group) == "yyy"
    shutil.rmtree(c.snapshot_base)


async def test_file():
    a = "中文 测试 abc"
    data_id = "com.alibaba.cloud.acm:sample-app.properties"
    group = "sandbox"
    key = "+".join([data_id, group, NAMESPACE])
    files.delete_file(aioacm.DEFAULTS["SNAPSHOT_BASE"], key)
    files.save_file(aioacm.DEFAULTS["SNAPSHOT_BASE"], key, a)
    assert a == files.read_file(aioacm.DEFAULTS["SNAPSHOT_BASE"], key)


async def test_publish():
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    data_id = "com.alibaba.cloud.acm:sample-app.properties"
    group = "sandbox"
    content = "test"
    await c.publish(data_id, group, content)


async def test_publish_remove():
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    data_id = "com.alibaba.cloud.acm:sample-app.properties"
    group = "sandbox"
    content = u"test中文"
    assert await c.remove(data_id, group)
    await asyncio.sleep(0.5)
    assert await c.get(data_id, group) is None
    await c.publish(data_id, group, content)
    await asyncio.sleep(0.5)
    assert await c.get(data_id, group) == content


async def test_list_all():
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    c.set_debugging()
    assert len(await c.list_all()) > 1


async def test_kms_encrypt():
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    c.set_options(kms_enabled=True, kms_ak=KMS_AK, kms_secret=KMS_SECRET,
                  region_id=REGION_ID, key_id=KEY_ID)
    assert c.encrypt("中文") != "中文"


async def test_kms_decrypt():
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    c.set_options(kms_enabled=True, kms_ak=KMS_AK, kms_secret=KMS_SECRET,
                  region_id=REGION_ID, key_id=KEY_ID)
    a = c.encrypt("test")
    assert c.decrypt(a) == "test"


async def test_key_encrypt():
    c = aioacm.ACMClient(ENDPOINT, NAMESPACE, AK, SK)
    c.set_options(kms_enabled=True, kms_ak=KMS_AK, kms_secret=KMS_SECRET,
                  region_id=REGION_ID, key_id=KEY_ID)
    value = "test"
    assert await c.publish("test_python-cipher", None, value)
    assert await c.get("test_python-cipher", None) == value
