# coding: utf8

import random
import socket
import logging

from asyncio import TimeoutError
from aiohttp import ClientSession, ClientError

LOGGER = logging.getLogger("aioacm")

ADDRESS_URL_PTN = "http://%s/diamond-server/diamond"

ADDRESS_SERVER_TIMEOUT = 3  # in seconds


def is_ipv4_address(address):
    try:
        socket.inet_aton(address)
    except socket.error:
        return False
    return True


async def get_server_list(endpoint: str, default_port: int = 8080,
                          cai_enabled: bool = True) -> list:
    logger = LOGGER.getChild("get-server-list")
    server_list = list()
    if not cai_enabled:
        logger.info(
            "cai server is not used, regard endpoint:%s "
            "as server.",
            endpoint
        )
        content = endpoint
    if ':' not in endpoint:
        content = ':'.join([endpoint, str(default_port)])
    else:
        try:
            async with ClientSession() as request:
                async with request.get(ADDRESS_URL_PTN % endpoint,
                                       timeout=ADDRESS_SERVER_TIMEOUT) as resp:
                    content = await resp.text()
            logger.debug("content from endpoint:%s", content)
        except ClientError as e:
            logger.error(
                "get server from %s failed.",
                endpoint,
                exc_info=e
            )
            return server_list
        except TimeoutError:
            logger.error(
                "Timeout(%s) when get server from %s.",
                ADDRESS_SERVER_TIMEOUT,
                ADDRESS_URL_PTN % endpoint
            )
            return server_list

    if content:
        for server_info in content.strip().split("\n"):
            sp = server_info.strip().split(":")
            if len(sp) == 1:
                server_list.append(
                    (sp[0], default_port, is_ipv4_address(sp[0]))
                )
            else:
                try:
                    server_list.append(
                        (sp[0], int(sp[1]), is_ipv4_address(sp[0]))
                    )
                except ValueError:
                    logger.warning(
                        "bad server address:%s ignored",
                        server_info
                    )

    random.shuffle(server_list)

    return server_list
