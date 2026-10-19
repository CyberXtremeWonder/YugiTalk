"""Windows 使用 Psycopg 支持的事件循环，适用于普通启动与热重载。"""

import asyncio


def selector_loop_factory() -> asyncio.AbstractEventLoop:
    """Uvicorn 自定义工厂直接返回循环实例，不能返回循环类。"""
    return asyncio.SelectorEventLoop()
