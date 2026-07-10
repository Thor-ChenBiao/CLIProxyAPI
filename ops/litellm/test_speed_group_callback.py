import unittest
from types import SimpleNamespace

from ops.litellm.speed_group_callback import speed_group_callback


class SpeedGroupCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, metadata, data):
        auth = SimpleNamespace(metadata=metadata)
        return await speed_group_callback.async_pre_call_hook(
            auth,
            None,
            dict(data),
            "responses",
        )

    async def test_fast_key_defaults_to_priority(self):
        result = await self.apply({"speed_group": "fast"}, {})

        self.assertEqual(result["service_tier"], "priority")

    async def test_explicit_standard_wins_for_fast_key(self):
        result = await self.apply(
            {"speed_group": "fast"},
            {"service_tier": "default"},
        )

        self.assertEqual(result["service_tier"], "default")

    async def test_explicit_fast_wins_for_standard_key(self):
        result = await self.apply(
            {"speed_group": "standard"},
            {"service_tier": "priority"},
        )

        self.assertEqual(result["service_tier"], "priority")

    async def test_missing_or_invalid_group_stays_standard(self):
        self.assertNotIn("service_tier", await self.apply({}, {}))
        self.assertNotIn(
            "service_tier",
            await self.apply({"speed_group": "other"}, {}),
        )

    async def test_null_service_tier_uses_key_default(self):
        result = await self.apply(
            {"speed_group": "fast"},
            {"service_tier": None},
        )

        self.assertEqual(result["service_tier"], "priority")


if __name__ == "__main__":
    unittest.main()
