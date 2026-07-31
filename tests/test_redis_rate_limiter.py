import pytest

from app.auth.rate_limit import RedisLoginRateLimiter
from app.auth.security import AuthenticationError


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.deleted = []

    async def eval(self, script, number_of_keys, key, window):
        self.values[key] = self.values.get(key, 0) + 1
        return self.values[key]

    async def delete(self, key):
        self.deleted.append(key)
        self.values.pop(key, None)


@pytest.mark.asyncio
async def test_redis_login_rate_limiter_is_shared_and_resets():
    redis = FakeRedis()
    first = RedisLoginRateLimiter(redis, attempts=2, window_seconds=60, prefix="test:login")
    second = RedisLoginRateLimiter(redis, attempts=2, window_seconds=60, prefix="test:login")

    await first.check("tenant:alice")
    await second.check("tenant:alice")
    with pytest.raises(AuthenticationError, match="Too many"):
        await first.check("tenant:alice")

    assert "alice" not in next(iter(redis.values))
    await second.reset("tenant:alice")
    await first.check("tenant:alice")
