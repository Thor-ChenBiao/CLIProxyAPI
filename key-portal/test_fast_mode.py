import unittest

import fast_mode


class FakePipeline:
    def __init__(self, client):
        self.client = client
        self.operations = []

    def set(self, key, value):
        self.operations.append((key, value))
        return self

    def execute(self):
        for key, value in self.operations:
            self.client.values[key] = value
        return [True] * len(self.operations)


class FakeRedis:
    def __init__(self, values=None):
        self.values = dict(values or {})

    def get(self, key):
        return self.values.get(key)

    def pipeline(self, transaction=True):
        if not transaction:
            raise AssertionError("Fast mode writes must be transactional")
        return FakePipeline(self)


class FastModeStoreTests(unittest.TestCase):
    def test_missing_key_defaults_to_enabled(self):
        store = fast_mode.FastModeStore(redis_client=FakeRedis())

        state = store.load()

        self.assertTrue(state["enabled"])
        self.assertEqual(state["source"], "default")

    def test_save_persists_boolean_and_metadata(self):
        client = FakeRedis()
        store = fast_mode.FastModeStore(redis_client=client)

        saved = store.save(False, updated_by="admin@example.com", reason="test")
        loaded = store.load()

        self.assertFalse(saved["enabled"])
        self.assertFalse(loaded["enabled"])
        self.assertEqual(loaded["source"], "redis")
        self.assertEqual(loaded["updated_by"], "admin@example.com")
        self.assertEqual(loaded["reason"], "test")

    def test_invalid_redis_value_is_reported_unavailable(self):
        store = fast_mode.FastModeStore(
            redis_client=FakeRedis({fast_mode.DEFAULT_REDIS_KEY: "maybe"}),
        )

        with self.assertRaises(fast_mode.FastModeUnavailable):
            store.load()

    def test_missing_redis_configuration_is_reported_unavailable(self):
        store = fast_mode.FastModeStore()

        with self.assertRaises(fast_mode.FastModeUnavailable):
            store.load()


if __name__ == "__main__":
    unittest.main()
