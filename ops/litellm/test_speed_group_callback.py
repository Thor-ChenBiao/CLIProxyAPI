import unittest
from types import SimpleNamespace

from ops.litellm.speed_group_callback import FAST_MODE_REDIS_KEY, SpeedGroupCallback


class FakeRedis:
    def __init__(self, value):
        self.value = value
        self.get_calls = 0

    def get(self, key):
        if key != FAST_MODE_REDIS_KEY:
            raise AssertionError(f"unexpected Redis key: {key}")
        self.get_calls += 1
        if isinstance(self.value, Exception):
            raise self.value
        return self.value


class SpeedGroupCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, metadata, data, global_enabled="1"):
        auth = SimpleNamespace(metadata=metadata)
        callback = SpeedGroupCallback(
            redis_client=FakeRedis(global_enabled),
            cache_seconds=5,
        )
        return await callback.async_pre_call_hook(
            auth,
            None,
            dict(data),
            "responses",
        )

    async def test_fast_key_defaults_to_priority(self):
        result = await self.apply({"speed_group": "fast"}, {})

        self.assertEqual(result["service_tier"], "priority")

    async def test_fast_key_explicit_default_is_overridden_when_global_enabled(self):
        result = await self.apply(
            {"speed_group": "fast"},
            {"service_tier": "default"},
        )

        self.assertEqual(result["service_tier"], "priority")

    async def test_standard_key_cannot_request_priority(self):
        result = await self.apply(
            {"speed_group": "standard"},
            {"service_tier": "priority"},
        )

        self.assertEqual(result["service_tier"], "default")

    async def test_missing_or_invalid_group_is_forced_to_default(self):
        self.assertEqual((await self.apply({}, {}))["service_tier"], "default")
        self.assertEqual(
            (await self.apply({"speed_group": "other"}, {}))["service_tier"],
            "default",
        )

    async def test_null_service_tier_uses_key_default(self):
        result = await self.apply(
            {"speed_group": "fast"},
            {"service_tier": None},
        )

        self.assertEqual(result["service_tier"], "priority")

    async def test_global_disabled_forces_fast_key_to_default(self):
        result = await self.apply(
            {"speed_group": "fast"},
            {"service_tier": "priority"},
            global_enabled="0",
        )

        self.assertEqual(result["service_tier"], "default")

    async def test_missing_redis_key_preserves_current_enabled_behavior(self):
        result = await self.apply({"speed_group": "fast"}, {}, global_enabled=None)

        self.assertEqual(result["service_tier"], "priority")

    async def test_cold_redis_failure_fails_closed(self):
        result = await self.apply(
            {"speed_group": "fast"},
            {},
            global_enabled=RuntimeError("Redis unavailable"),
        )

        self.assertEqual(result["service_tier"], "default")

    async def test_cached_state_is_used_during_redis_failure(self):
        now = [10.0]
        redis_client = FakeRedis("1")
        callback = SpeedGroupCallback(
            redis_client=redis_client,
            cache_seconds=5,
            clock=lambda: now[0],
        )
        auth = SimpleNamespace(metadata={"speed_group": "fast"})

        first = await callback.async_pre_call_hook(auth, None, {}, "responses")
        redis_client.value = RuntimeError("Redis unavailable")
        now[0] = 20.0
        second = await callback.async_pre_call_hook(auth, None, {}, "responses")
        now[0] = 21.0
        third = await callback.async_pre_call_hook(auth, None, {}, "responses")

        self.assertEqual(first["service_tier"], "priority")
        self.assertEqual(second["service_tier"], "priority")
        self.assertEqual(third["service_tier"], "priority")
        self.assertEqual(redis_client.get_calls, 2)


if __name__ == "__main__":
    unittest.main()
