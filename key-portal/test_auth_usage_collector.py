import json
import unittest
from unittest.mock import patch

from auth_usage_collector import AuthUsageCollector


class FakeStore:
    def __init__(self):
        self.calls = []
        self.reset_calls = []

    def reset_collection_coverage(self, node_names):
        self.reset_calls.append(list(node_names))
        return True

    def ingest_results(self, results, collection_complete=True, coverage_nodes=None):
        self.calls.append((results, collection_complete, coverage_nodes))
        return {"records": sum(len(payload) for _, payload, _ in results), "buckets": 1, "errors": []}


class AuthUsageCollectorTests(unittest.TestCase):
    def test_start_preserves_coverage_until_a_real_collection_gap(self):
        class FakeThread:
            def __init__(self, *args, **kwargs):
                pass

            def start(self):
                pass

        store = FakeStore()
        collector = AuthUsageCollector(
            store=store,
            nodes=[{"name": "node-a", "url": "http://127.0.0.1:8317"}],
            management_key="secret",
        )

        with patch("auth_usage_collector.threading.Thread", FakeThread):
            self.assertTrue(collector.start())

        self.assertEqual(store.reset_calls, [])

    def test_agent_builds_single_local_node_collector(self):
        from auth_usage_agent import build_collector

        store, collector = build_collector(
            node_name="node-b",
            node_url="http://127.0.0.1:8317",
            database_url="postgresql://user:pass@example/db",
            management_key="secret",
        )

        self.assertTrue(store.database_url.startswith("postgresql://"))
        self.assertEqual(collector.node_names, ["node-b"])

    def test_connection_kwargs_follow_node_url_scheme(self):
        plain = AuthUsageCollector.connection_kwargs(
            {"url": "http://127.0.0.1:8317"}, "management-secret"
        )
        tls = AuthUsageCollector.connection_kwargs(
            {"url": "https://172.31.26.28:8443"}, "management-secret"
        )

        self.assertEqual((plain["host"], plain["port"], plain["ssl"]), ("127.0.0.1", 8317, False))
        self.assertEqual((tls["host"], tls["port"], tls["ssl"]), ("172.31.26.28", 8443, True))
        self.assertEqual(plain["password"], "management-secret")
        self.assertEqual(tls["ssl_cert_reqs"], "none")

    def test_flush_batches_records_by_node_and_reports_coverage(self):
        store = FakeStore()
        collector = AuthUsageCollector(
            store=store,
            nodes=[{"name": "node-a", "url": "http://127.0.0.1:8317"}, {"name": "node-b", "url": "https://node-b:8443"}],
            management_key="secret",
        )
        collector.set_node_connected("node-a", True)
        collector.set_node_connected("node-b", True)
        collector.enqueue_message("node-a", json.dumps({"timestamp": "2026-07-13T15:00:00Z", "auth_index": "a"}).encode())
        collector.enqueue_message("node-b", json.dumps({"timestamp": "2026-07-13T15:00:01Z", "auth_index": "b"}).encode())

        result = collector.flush_once()

        self.assertEqual(result["records"], 2)
        results, collection_complete, coverage_nodes = store.calls[-1]
        self.assertTrue(collection_complete)
        self.assertEqual(coverage_nodes, ["node-a", "node-b"])
        self.assertEqual({node: len(records) for node, records, _ in results}, {"node-a": 1, "node-b": 1})

    def test_control_messages_are_ignored_and_disconnection_marks_gap(self):
        store = FakeStore()
        collector = AuthUsageCollector(
            store=store,
            nodes=[{"name": "node-a", "url": "http://127.0.0.1:8317"}],
            management_key="secret",
        )
        collector.enqueue_message("node-a", b'{"support_refresh":true}')
        collector.set_node_connected("node-a", False)

        collector.flush_once()

        results, collection_complete, coverage_nodes = store.calls[-1]
        self.assertEqual(results, [])
        self.assertFalse(collection_complete)
        self.assertEqual(coverage_nodes, ["node-a"])


if __name__ == "__main__":
    unittest.main()
