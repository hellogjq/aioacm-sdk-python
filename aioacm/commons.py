# coding: utf8

import atexit
import asyncio
from asyncio import iscoroutine


def synchronized_with_attr(attr_name):

    def decorator(func):

        async def locked_func(*args, **kws):
            self = args[0]
            lock = getattr(self, attr_name)
            async with lock:
                result = func(*args, **kws)
                if iscoroutine(result):
                    return await result
                return result

        def synced_func(*args, **kw):
            future = (asyncio
                      .ensure_future(locked_func(*args, **kw)))
            atexit.register(future.cancel)
            future.add_done_callback(
                lambda f: atexit.unregister(future.cancel)
            )

        return synced_func

    return decorator


def truncate(ori_str, length=100):
    if not ori_str:
        return ""
    return ori_str[:length] + "..." if len(ori_str) > length else ori_str
