import unittest

from usage_summary_snapshot import UsageSummarySnapshot


class DeferredThread:
    def __init__(self, target, queue):
        self.target = target
        self.queue = queue

    def start(self):
        self.queue.append(self.target)


class UsageSummarySnapshotTests(unittest.TestCase):
    def snapshot(self, now, queued):
        return UsageSummarySnapshot(
            ttl_seconds=2,
            clock=lambda: now[0],
            thread_factory=lambda target: DeferredThread(target, queued),
        )

    def test_cold_load_stores_snapshot(self):
        now = [10.0]
        queued = []
        snapshot = self.snapshot(now, queued)

        data, error = snapshot.get(lambda: {"today": "2026-07-13", "today_tokens": 10})

        self.assertIsNone(error)
        self.assertEqual(data["today_tokens"], 10)
        self.assertEqual(data["cache_age_seconds"], 0)
        self.assertFalse(data["cache_refreshing"])
        self.assertEqual(queued, [])

    def test_stale_reads_start_only_one_background_refresh(self):
        now = [10.0]
        queued = []
        snapshot = self.snapshot(now, queued)
        snapshot.get(lambda: {"today": "2026-07-13", "today_tokens": 10})
        now[0] = 13.0

        first, first_error = snapshot.get(lambda: {"today": "2026-07-13", "today_tokens": 20})
        second, second_error = snapshot.get(lambda: {"today": "2026-07-13", "today_tokens": 30})

        self.assertIsNone(first_error)
        self.assertIsNone(second_error)
        self.assertEqual(first["today_tokens"], 10)
        self.assertEqual(second["today_tokens"], 10)
        self.assertTrue(first["cache_refreshing"])
        self.assertEqual(len(queued), 1)

        queued.pop()()
        refreshed, refreshed_error = snapshot.get(lambda: self.fail("fresh cache must not reload"))

        self.assertIsNone(refreshed_error)
        self.assertEqual(refreshed["today_tokens"], 20)
        self.assertFalse(refreshed["cache_refreshing"])

    def test_failed_background_refresh_preserves_valid_snapshot(self):
        now = [10.0]
        queued = []
        snapshot = self.snapshot(now, queued)
        snapshot.get(lambda: {"today": "2026-07-13", "today_tokens": 10})
        now[0] = 13.0

        stale, error = snapshot.get(lambda: (_ for _ in ()).throw(RuntimeError("PG timeout")))
        queued.pop()()
        preserved, preserved_error = snapshot.get(lambda: self.fail("preserved cache must remain available"))

        self.assertIsNone(error)
        self.assertEqual(stale["today_tokens"], 10)
        self.assertIsNone(preserved_error)
        self.assertEqual(preserved["today_tokens"], 10)
        self.assertEqual(preserved["cache_refresh_error"], "PG timeout")

    def test_cold_load_failure_returns_error_instead_of_zero_snapshot(self):
        now = [10.0]
        queued = []
        snapshot = self.snapshot(now, queued)

        data, error = snapshot.get(lambda: (_ for _ in ()).throw(RuntimeError("PG timeout")))

        self.assertIsNone(data)
        self.assertEqual(error, "PG timeout")
        self.assertEqual(queued, [])


if __name__ == "__main__":
    unittest.main()
