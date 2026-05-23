import os
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import app as portal_app
import database as db
import usage_sync


SCHEMA_SQL = """
CREATE TABLE daily_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL UNIQUE,
    total_requests INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE user_usage (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,
    user_email TEXT NOT NULL,
    api_key TEXT NOT NULL,
    total_requests INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    input_tokens INTEGER DEFAULT 0,
    output_tokens INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(date, user_email, api_key)
);
"""


class TempUsageDb(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.NamedTemporaryFile(delete=False)
        self.tmp.close()
        self.old_db_file = db.DB_FILE
        db.DB_FILE = self.tmp.name
        with sqlite3.connect(db.DB_FILE) as conn:
            conn.executescript(SCHEMA_SQL)
            conn.commit()
        db.ensure_indexes()

    def tearDown(self):
        db.DB_FILE = self.old_db_file
        os.unlink(self.tmp.name)


class UsageMergeTests(unittest.TestCase):
    def test_merge_usage_payloads_combines_nodes_and_preserves_details(self):
        detail = {
            "timestamp": datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ"),
            "tokens": {"input_tokens": 3, "output_tokens": 7, "total_tokens": 10},
            "failed": False,
        }
        results = [
            ("old", {"usage": {
                "total_requests": 1,
                "success_count": 1,
                "failure_count": 0,
                "total_tokens": 10,
                "tokens_by_day": {"2026-05-11": 10},
                "requests_by_day": {"2026-05-11": 1},
                "apis": {"k1": {"total_requests": 1, "total_tokens": 10, "models": {"m": {"total_requests": 1, "total_tokens": 10, "details": [detail]}}}},
            }}, None),
            ("node-b", {"usage": {
                "total_requests": 2,
                "success_count": 1,
                "failure_count": 1,
                "total_tokens": 20,
                "tokens_by_day": {"2026-05-11": 20},
                "requests_by_day": {"2026-05-11": 2},
                "apis": {"k1": {"total_requests": 2, "total_tokens": 20, "models": {"m": {"total_requests": 2, "total_tokens": 20, "details": [detail, dict(detail, failed=True)]}}}},
            }}, None),
        ]

        merged = portal_app.merge_usage_payloads(results)
        usage = merged["usage"]

        self.assertEqual(usage["total_requests"], 3)
        self.assertEqual(usage["total_tokens"], 30)
        self.assertEqual(usage["tokens_by_day"]["2026-05-11"], 30)
        details = usage["apis"]["k1"]["models"]["m"]["details"]
        self.assertEqual(len(details), 3)
        self.assertEqual({item["node"] for item in details}, {"old", "node-b"})

    def test_call_management_api_all_returns_partial_success(self):
        nodes = [
            {"name": "old", "url": "http://old"},
            {"name": "node-b", "url": "http://node-b"},
        ]

        def fake_call(node, method, endpoint, data=None, timeout=30):
            if node["name"] == "old":
                return {"ok": True}, None
            return None, "timeout"

        with patch.object(portal_app, "CLIPROXY_NODES", nodes), patch.object(portal_app, "call_management_api_node", side_effect=fake_call):
            results = portal_app.call_management_api_all("GET", "/x", timeout=1)

        self.assertEqual(results[0], ("old", {"ok": True}, None))
        self.assertEqual(results[1], ("node-b", None, "timeout"))


class DatabaseTests(TempUsageDb):
    def test_upserts_are_monotonic(self):
        db.upsert_daily_usage("2026-05-11", 10, 9, 1, 100, 40, 60)
        db.upsert_daily_usage("2026-05-11", 5, 4, 0, 50, 20, 30)
        daily = db.get_daily_usage_history()[0]
        self.assertEqual(daily["total_requests"], 10)
        self.assertEqual(daily["total_tokens"], 100)

        db.upsert_user_usage("2026-05-11", "u@example.com", "k1", 3, 3, 0, 30, 10, 20)
        db.upsert_user_usage("2026-05-11", "u@example.com", "k1", 4, 4, 0, 40, 15, 25)
        user = db.get_user_key_usage_for_date("u@example.com", "2026-05-11")["k1"]
        self.assertEqual(user["total_requests"], 4)
        self.assertEqual(user["total_tokens"], 40)

    def test_usage_sync_uses_beijing_date_for_details(self):
        api_data = {"usage": {"tokens_by_day": {}, "requests_by_day": {}, "apis": {
            "k1": {"total_requests": 1, "total_tokens": 10, "models": {"m": {"details": [{
                "timestamp": "2026-05-10T16:30:00Z",
                "tokens": {"input_tokens": 3, "output_tokens": 7, "total_tokens": 10},
                "failed": False,
            }]}}}
        }}}

        ok, stats = usage_sync.sync_usage_to_database(api_data, {"k1": "u@example.com"})

        self.assertTrue(ok, stats)
        daily = db.get_daily_usage_history()[0]
        self.assertEqual(daily["date"], "2026-05-11")
        user = db.get_user_key_usage_for_date("u@example.com", "2026-05-11")["k1"]
        self.assertEqual(user["total_tokens"], 10)


class FlaskSmokeTests(unittest.TestCase):
    def test_read_only_usage_routes(self):
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "get_usage_summary_cached", return_value=({"total_tokens": 1, "total_requests": 1}, None)), \
             patch.object(portal_app, "get_usage_history_aggregated", return_value={"history": [], "by_month": {}, "by_year": {}}), \
             patch.object(portal_app, "get_cluster_usage_summary", return_value={"usage": {}}), \
             patch.object(portal_app, "get_usage_stats_cached", return_value=({"usage": {}}, None)), \
             patch.object(portal_app, "build_all_users_stats_response", return_value={"users": [], "summary": {}, "aggregation": "total"}), \
             patch.object(portal_app, "get_auth_stats_cached", return_value={"auth_files": [], "nodes": {}, "errors": []}):
            self.assertEqual(client.get("/api/usage-summary").status_code, 401)
            with patch.object(portal_app, "current_portal_session", return_value={"email": "u@example.com", "user": {}}):
                self.assertEqual(client.get("/api/usage-summary").status_code, 200)
                self.assertEqual(client.get("/api/usage-history").status_code, 200)
                self.assertEqual(client.get("/api/all-users-stats").status_code, 403)
                self.assertEqual(client.get("/api/auth-stats").status_code, 403)
            with patch.object(portal_app, "current_portal_session", return_value={"email": "biao.chen@zilliz.com", "user": {}}):
                self.assertEqual(client.get("/api/all-users-stats").status_code, 200)
                self.assertEqual(client.get("/api/auth-stats").status_code, 200)

    def test_monitor_log_recent_entries_metadata_only(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            timestamp = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(os.path.join(tmpdir, "monitor-test.log"), "w", encoding="utf-8") as handle:
                handle.write(f"========== {timestamp} ==========\n")
                handle.write("Key: sk-test-1234567890\n")
                handle.write("Method: POST\n")
                handle.write("URL: /v1/chat/completions\n")
                handle.write("Status: 200\n")
                handle.write("Request-Size: 10\n")
                handle.write("Response-Size: 20\n")
                handle.write("\n--- REQUEST BODY ---\n")
                handle.write("secret request body\n")
                handle.write("\n--- RESPONSE ---\n")
                handle.write("secret response body\n")
                handle.write("\n========== END ==========\n")

            rows = portal_app.monitor_log_recent_entries("sk-test-1234567890", hours=1, log_dir=tmpdir)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["key"], "sk-tes...7890")
        self.assertEqual(rows[0]["url"], "/v1/chat/completions")
        self.assertNotIn("request_body", rows[0])
        self.assertNotIn("response_body", rows[0])
        self.assertNotIn("secret request body", str(rows[0]))

    def test_monitor_log_recent_entries_filters_old_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            timestamp = (datetime.utcnow() - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
            with open(os.path.join(tmpdir, "monitor-old.log"), "w", encoding="utf-8") as handle:
                handle.write(f"========== {timestamp} ==========\n")
                handle.write("Key: sk-test-1234567890\n")
                handle.write("Status: 200\n")
                handle.write("========== END ==========\n")

            rows = portal_app.monitor_log_recent_entries("sk-test-1234567890", hours=1, log_dir=tmpdir)

        self.assertEqual(rows, [])

    def test_user_monitor_recent_requests_returns_metadata_only(self):
        user_data = {
            "users": {"u@example.com": {"api_keys": ["sk-test-1234567890"]}},
            "keys": {"sk-test-1234567890": {"email": "u@example.com", "label": "test-key"}},
        }
        rows = [{
            "start_time": "2026-05-23T01:00:00Z",
            "end_time": "2026-05-23T01:00:01Z",
            "key_label": "test-key",
            "user_email": "u@example.com",
            "model": "gpt-5.5",
            "status": "success",
            "total_tokens": 10,
            "input_tokens": 4,
            "output_tokens": 6,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "spend_usd": 0.001,
            "latency_ms": 1000,
            "call_type": "acompletion",
            "request_id": "req-1",
            "user_api_base": "",
        }]
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@example.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=user_data), \
             patch.object(portal_app, "litellm_recent_requests", return_value=rows):
            response = client.get("/api/user-monitor/recent-requests?api_key=sk-test-1234567890")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["api_key"], "sk-tes...7890")
        self.assertEqual(payload["source"], "litellm_spendlogs")
        self.assertEqual(payload["requests"][0]["model"], "gpt-5.5")
        self.assertNotIn("request_body", payload["requests"][0])
        self.assertNotIn("response_body", payload["requests"][0])

    def test_user_monitor_reconcile_compares_counts(self):
        user_data = {
            "users": {"u@example.com": {"api_keys": ["sk-test-1234567890"]}},
            "keys": {"sk-test-1234567890": {"email": "u@example.com", "label": "test-key"}},
        }
        litellm_rows = [
            {"status": "success", "model": "gpt-5.5"},
            {"status": "failure", "model": "gpt-5.5"},
        ]
        monitor_rows = [
            {"status": "200", "url": "/v1/chat/completions"},
            {"status": "500", "url": "/v1/chat/completions"},
        ]
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@example.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=user_data), \
             patch.object(portal_app, "litellm_recent_requests", return_value=litellm_rows), \
             patch.object(portal_app, "monitor_log_recent_entries", return_value=monitor_rows):
            response = client.get("/api/user-monitor/reconcile?api_key=sk-test-1234567890&hours=1")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["litellm"]["requests"], 2)
        self.assertEqual(payload["monitor_logs"]["requests"], 2)
        self.assertEqual(payload["difference"], 0)
        self.assertTrue(payload["within_one_percent"])


if __name__ == "__main__":
    unittest.main()
