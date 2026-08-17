from __future__ import annotations

import hashlib

from redis.asyncio import Redis

from app.auth.security import AuthenticationError

_INCREMENT_WINDOW = """
local count = redis.call('INCR', KEYS[1])
if count == 1 then
  redis.call('EXPIRE', KEYS[1], ARGV[1])
end
return count
"""


class RedisLoginRateLimiter:
    def __init__(self, redis: Redis, attempts: int, window_seconds: int, prefix: str):
        self.redis = redis
        self.attempts = attempts
        self.window_seconds = window_seconds
        self.prefix = prefix

    def _key(self, identity: str) -> str:
        digest = hashlib.sha256(identity.casefold().encode()).hexdigest()
        return f"{self.prefix}:{digest}"

    async def check(self, identity: str) -> None:
        count = await self.redis.eval(
            _INCREMENT_WINDOW,
            1,
            self._key(identity),
            str(self.window_seconds),
        )
        if int(count) > self.attempts:
            raise AuthenticationError("Too many login attempts. Try again later.")

    async def reset(self, identity: str) -> None:
        await self.redis.delete(self._key(identity))
