"""Redis 原子登录限流，多个 API 实例共享计数，失败时关闭登录入口。"""

from collections.abc import Callable

from fastapi import HTTPException
from redis import Redis
from redis.exceptions import RedisError

from app.core.config import Settings
from app.identity.security import digest

LoginLimiter = Callable[[str, str], None]
COUNTER = """
local n = redis.call('INCR', KEYS[1])
if n == 1 then redis.call('EXPIRE', KEYS[1], 900) end
return n
"""


class RedisLoginLimiter:
    """利用共享计数限制登录尝试，存储异常时拒绝绕过限制。"""

    def __init__(self, settings: Settings, purpose: str = "login") -> None:
        """保存实例所需的依赖与配置，初始化后续执行使用的状态。"""
        self.settings = settings
        self.purpose = purpose

    def __call__(self, address: str, email: str) -> None:
        """按来源地址和邮箱累计登录尝试，并执行频率限制。"""
        secret = self.settings.session_secret.get_secret_value()
        try:
            with Redis.from_url(
                self.settings.redis_url.get_secret_value(),
                socket_timeout=3,
                socket_connect_timeout=3,
            ) as client:
                for value, limit in ((f"ip:{address}", 30), (f"account:{email}", 10)):
                    key = "yugitalk:" + self.purpose + ":" + digest(secret, "rate-limit", value)
                    count = client.eval(COUNTER, 1, key)
                    if int(count) > limit:
                        raise HTTPException(429, headers={"Retry-After": "900"})
        except RedisError as error:
            raise HTTPException(503) from error
