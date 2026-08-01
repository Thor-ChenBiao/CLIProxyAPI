import unittest
from datetime import datetime, timezone


class RecordingCursor:
    def __init__(self, rows=None):
        self.statements = []
        self.rows = list(rows or [])

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, *args):
        self.statements.append((statement, args))

    def fetchall(self):
        return list(self.rows)


class RecordingConnection:
    def __init__(self, rows=None):
        self.cursor_instance = RecordingCursor(rows)

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def cursor(self):
        return self.cursor_instance

    def commit(self):
        pass


class AuthUsageStoreTests(unittest.TestCase):
    def test_nanosecond_timestamp_is_accepted_on_python_39(self):
        from auth_usage_store import AuthUsageStore

        rows = AuthUsageStore.aggregate_records("node-a", [{
            "timestamp": "2026-07-13T18:41:19.092048145Z",
            "auth_index": "auth-a",
            "source": "account@example.com",
            "tokens": {"total_tokens": 100},
            "failed": False,
        }])

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["requests"], 1)
        self.assertEqual(rows[0]["last_request_at"].isoformat(), "2026-07-13T18:41:19.092048+00:00")

    def test_queue_records_are_compacted_into_minute_buckets(self):
        from auth_usage_store import AuthUsageStore

        records = [
            {
                "timestamp": "2026-07-13T15:04:10Z",
                "request_id": "request-1",
                "api_key": "secret-key-must-not-be-persisted",
                "auth_index": "auth-a",
                "source": "account@example.com",
                "tokens": {"input_tokens": 80, "output_tokens": 20, "total_tokens": 100},
                "failed": False,
            },
            {
                "timestamp": "2026-07-13T15:04:50Z",
                "request_id": "request-2",
                "api_key": "secret-key-must-not-be-persisted",
                "auth_index": "auth-a",
                "source": "account@example.com",
                "tokens": {"input_tokens": 40, "output_tokens": 10, "total_tokens": 50},
                "failed": True,
                "fail": {"status_code": 429, "body": "rate limited"},
            },
        ]

        rows = AuthUsageStore.aggregate_records("node-a", records)

        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["bucket_start"].isoformat(), "2026-07-13T15:04:00+00:00")
        self.assertEqual(row["requests"], 2)
        self.assertEqual(row["success"], 1)
        self.assertEqual(row["failure"], 1)
        self.assertEqual(row["tokens"], 150)
        self.assertEqual(row["input_tokens"], 120)
        self.assertEqual(row["output_tokens"], 30)
        self.assertEqual(row["last_error_status"], "429")
        self.assertEqual(row["last_error_message"], "rate limited")
        self.assertNotIn("api_key", row)

    def test_incomplete_collection_resets_coverage(self):
        from auth_usage_store import AuthUsageStore

        store = AuthUsageStore("")
        store.enabled = True
        store._schema_ready = True
        connection = RecordingConnection()
        store.connect = lambda: connection

        store.ingest_results([], collection_complete=False, coverage_nodes=["node-a"])

        state_statement, state_args = next(
            item for item in connection.cursor_instance.statements
            if "key_portal_auth_usage_node_state" in item[0]
        )
        self.assertIn("continuous_since = now()", state_statement)
        self.assertEqual(state_args[0], ("node-a",))

    def test_quota_window_usage_uses_collector_coverage(self):
        from auth_usage_store import AuthUsageStore

        utc = timezone.utc
        connection = RecordingConnection([{
            "node": "node-a",
            "auth_index": "auth-a",
            "start_at": datetime(2026, 7, 29, 4, 0, tzinfo=utc),
            "end_at": datetime(2026, 8, 5, 4, 0, tzinfo=utc),
            "requests": 12,
            "tokens": 3456,
            "observed_start_at": datetime(2026, 7, 29, 4, 1, tzinfo=utc),
            "observed_end_at": datetime(2026, 8, 1, 0, 0, tzinfo=utc),
            "continuous_since": datetime(2026, 7, 27, 0, 0, tzinfo=utc),
            "last_collected_at": datetime(2026, 8, 1, 0, 5, tzinfo=utc),
            "now_utc": datetime(2026, 8, 1, 0, 5, tzinfo=utc),
        }])
        store = AuthUsageStore("")
        store.enabled = True
        store._schema_ready = True
        store.connect = lambda: connection

        result = store.load_quota_windows([{
            "node": "node-a",
            "auth_index": "auth-a",
            "start_at": "2026-07-29T04:00:00Z",
            "end_at": "2026-08-05T04:00:00Z",
        }])

        row = result["node-a|auth-a"]
        self.assertTrue(row["complete"])
        self.assertEqual(row["requests"], 12)
        self.assertEqual(row["tokens"], 3456)
        self.assertEqual(row["required_seconds"], 245100)
        self.assertEqual(row["coverage_seconds"], 245100)
        query = connection.cursor_instance.statements[-1][0]
        self.assertIn("usage.bucket_start >= requested.start_at", query)
        self.assertIn("key_portal_auth_usage_node_state", query)


if __name__ == "__main__":
    unittest.main()
