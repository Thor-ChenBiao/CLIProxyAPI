import unittest


class RecordingCursor:
    def __init__(self):
        self.statements = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def execute(self, statement, *args):
        self.statements.append((statement, args))


class RecordingConnection:
    def __init__(self):
        self.cursor_instance = RecordingCursor()

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


if __name__ == "__main__":
    unittest.main()
