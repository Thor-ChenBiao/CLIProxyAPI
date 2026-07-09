import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

import app as portal_app
import usage_sync


class ModelGroupConfigTests(unittest.TestCase):
    def test_common_group_exposes_available_gpt_56_models(self):
        models = portal_app.LITELLM_MODEL_GROUPS["common"]["models"]

        self.assertIn("gpt-5.6-sol", models)
        self.assertIn("gpt-5.6-terra", models)
        self.assertNotIn("gpt-5.6-luna", models)

    def test_fable_alias_is_available_only_to_gpt_group(self):
        self.assertIn("claude-fable-5", portal_app.LITELLM_MODEL_GROUPS["common"]["models"])
        self.assertNotIn("claude-fable-5", portal_app.LITELLM_MODEL_GROUPS["claude"]["models"])


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

    def test_usage_queue_results_use_live_records_only(self):
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        first = [{
            "request_id": "req-1",
            "timestamp": now,
            "api_key": "k1",
            "model": "gpt-5.5",
            "tokens": {"total_tokens": 10},
        }]
        second = [{
            "request_id": "req-2",
            "timestamp": now,
            "api_key": "k1",
            "model": "gpt-5.5",
            "tokens": {"total_tokens": 20},
        }]

        merged_first = portal_app.merge_usage_queue_results([("test-node", first, None)])
        merged_second = portal_app.merge_usage_queue_results([("test-node", second, None)])

        self.assertEqual(merged_first["usage"]["total_requests"], 1)
        self.assertEqual(merged_second["usage"]["total_requests"], 1)
        self.assertEqual(merged_second["usage"]["total_tokens"], 20)
        self.assertEqual(merged_second["source"], "cliproxy_usage_queue_live")

    def test_usage_queue_records_convert_to_legacy_usage_payload(self):
        records = [
            {
                "timestamp": "2026-05-10T16:30:00Z",
                "api_key": "k1",
                "model": "gpt-5.5",
                "alias": "client-model",
                "provider": "openai",
                "latency_ms": 123,
                "tokens": {"input_tokens": 3, "output_tokens": 7, "total_tokens": 10},
                "failed": False,
            },
            {
                "timestamp": "2026-05-10T16:40:00Z",
                "api_key": "k1",
                "model": "gpt-5.5",
                "tokens": {"input_tokens": 1, "output_tokens": 2, "total_tokens": 3},
                "failed": True,
            },
        ]

        payload = portal_app.usage_payload_from_queue_records(records)
        usage = payload["usage"]

        self.assertEqual(usage["total_requests"], 2)
        self.assertEqual(usage["success_count"], 1)
        self.assertEqual(usage["failure_count"], 1)
        self.assertEqual(usage["total_tokens"], 13)
        self.assertEqual(usage["tokens_by_day"], {"2026-05-11": 13})
        details = usage["apis"]["k1"]["models"]["client-model"]["details"]
        self.assertEqual(details[0]["latency_ms"], 123)
        self.assertEqual(details[0]["tokens"]["input_tokens"], 3)


class AuthStatsTests(unittest.TestCase):
    def test_duplicate_auth_index_and_account_are_matched_by_node(self):
        now = datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%SZ")
        usage_payload = {
            "usage": {
                "apis": {
                    "k1": {
                        "models": {
                            "m": {
                                "details": [
                                    {
                                        "node": "node-a",
                                        "timestamp": now,
                                        "source": "openai-4@zasdas.com",
                                        "auth_index": "shared-index",
                                        "tokens": {"total_tokens": 100},
                                        "failed": False,
                                    },
                                    {
                                        "node": "node-c",
                                        "timestamp": now,
                                        "source": "openai-4@zasdas.com",
                                        "auth_index": "shared-index",
                                        "tokens": {"total_tokens": 200},
                                        "failed": False,
                                    },
                                ],
                            },
                        },
                    },
                },
            },
        }
        files = [
            {"node": "node-a", "account": "openai-4@zasdas.com", "auth_index": "shared-index", "provider": "test"},
            {"node": "node-c", "account": "openai-4@zasdas.com", "auth_index": "shared-index", "provider": "test"},
        ]
        service = portal_app.auth_stats_service.AuthStatsService(
            portal_state=object(),
            nodes=[{"name": "node-a", "url": "http://node-a"}, {"name": "node-c", "url": "http://node-c"}],
            call_management_api_node=lambda *args, **kwargs: ({}, None),
            get_cluster_usage=lambda: usage_payload,
            get_cluster_auth_files=lambda: (files, []),
            usage_summary_loader=lambda: ({}, None),
            parse_detail_time=portal_app.parse_detail_time,
            parse_detail_time_utc=portal_app.parse_detail_time_utc,
            build_token_breakdown=portal_app.build_token_breakdown,
        )

        stats = service.build()["auth_files"]
        by_node = {item["node"]: item for item in stats}

        self.assertEqual(by_node["node-a"]["total"]["requests"], 1)
        self.assertEqual(by_node["node-a"]["total"]["tokens"], 100)
        self.assertEqual(by_node["node-c"]["total"]["requests"], 1)
        self.assertEqual(by_node["node-c"]["total"]["tokens"], 200)

    def auth_stats_service(self):
        return portal_app.auth_stats_service.AuthStatsService(
            portal_state=object(),
            nodes=[],
            call_management_api_node=lambda *args, **kwargs: ({}, None),
            get_cluster_usage=lambda: {},
            get_cluster_auth_files=lambda: ([], []),
            usage_summary_loader=lambda: ({}, None),
            parse_detail_time=portal_app.parse_detail_time,
            parse_detail_time_utc=portal_app.parse_detail_time_utc,
            build_token_breakdown=portal_app.build_token_breakdown,
        )

    def test_today_quota_usage_does_not_extrapolate_from_one_sample(self):
        service = self.auth_stats_service()
        stats = {}
        for index in range(20):
            stats[str(index)] = {
                "today": {"tokens": 0},
                "quota_window": {"tokens": 49500 if index == 0 else 0, "source_window": "last_7d"},
                "quota": {"windows": {"last_7d": {"used_percent": 100, "limit_window_seconds": 7 * 24 * 3600}}},
            }

        result = service.build_today_quota_usage(stats, "2026-05-29", 8_320_000_000)

        self.assertEqual(result["inference_status"], "insufficient_history_coverage")
        self.assertFalse(result["configured"])
        self.assertEqual(result["total_daily_token_limit"], 0)
        self.assertEqual(result["usage_percent"], 0)
        self.assertEqual(result["min_inferred_samples"], 4)
        self.assertEqual(result["single_account_daily_token_limit"], 0)

    def test_today_quota_usage_requires_full_window_history(self):
        service = self.auth_stats_service()
        stats = {}
        for index in range(20):
            stats[str(index)] = {
                "today": {"tokens": 0},
                "quota_window": {
                    "tokens": 49_500,
                    "source_window": "last_7d",
                    "start_at": "2026-05-29T00:00:00Z",
                    "reset_at": "2026-05-29T01:00:00Z",
                },
                "quota": {"windows": {"last_7d": {"used_percent": 100, "limit_window_seconds": 7 * 24 * 3600}}},
            }

        result = service.build_today_quota_usage(stats, "2026-05-29", 8_320_000_000)

        self.assertEqual(result["inference_status"], "insufficient_history_coverage")
        self.assertFalse(result["configured"])
        self.assertEqual(result["total_daily_token_limit"], 0)
        self.assertEqual(result["usage_percent"], 0)
        self.assertEqual(result["partial_history_count"], 20)



class LiteLLMKeyTotalsTests(unittest.TestCase):
    def test_user_key_stats_for_date_filters_spendlogs_by_user_tokens(self):
        captured = []

        def fake_psql_json(sql, timeout=15):
            captured.append(sql)
            return []

        with patch.object(portal_app, "litellm_psql_json", side_effect=fake_psql_json), \
             patch.object(portal_app, "beijing_today", return_value="2026-07-09"):
            portal_app.litellm_user_key_stats_for_date("u@example.com", "2026-07-08")

        self.assertIn("user_tokens AS", captured[0])
        self.assertIn('"LiteLLM_SpendLogs"', captured[0])
        self.assertIn("s.api_key = ut.token", captured[0])

    def test_model_group_usage_uses_daily_history_and_today_spendlogs(self):
        captured = []

        def fake_psql_json(sql, timeout=15):
            captured.append(sql)
            return []

        with patch.object(portal_app, "litellm_psql_json", side_effect=fake_psql_json):
            portal_app.litellm_usage_by_model_group(7)

        self.assertIn("daily_rows AS", captured[0])
        self.assertIn("today_rows AS", captured[0])
        self.assertIn('"LiteLLM_DailyUserSpend"', captured[0])
        self.assertIn('"LiteLLM_SpendLogs"', captured[0])

    def test_usage_history_uses_daily_history_and_today_spendlogs(self):
        captured = []

        def fake_psql_json(sql, timeout=15):
            captured.append(sql)
            return {"history": [], "by_month": {}, "by_year": {}}

        with patch.object(portal_app, "litellm_psql_json", side_effect=fake_psql_json):
            portal_app.litellm_usage_history_aggregated(120)

        self.assertIn("daily_rows AS", captured[0])
        self.assertIn("today_rows AS", captured[0])
        self.assertIn('"LiteLLM_DailyUserSpend"', captured[0])
        self.assertIn('"LiteLLM_SpendLogs"', captured[0])
        self.assertEqual(len(captured), 1)

    def test_key_totals_filter_spend_rows_before_portal_key_creation(self):
        captured = {}

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql):
                if '"LiteLLM_SpendLogs"' in sql:
                    captured["sql"] = sql

            def fetchone(self):
                return [{}]

        class FakeConnection:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def cursor(self):
                return FakeCursor()

        fake_psycopg = types.SimpleNamespace(connect=lambda *args, **kwargs: FakeConnection())
        entry = {
            "key": "sk-test",
            "label": "test-label",
            "model_group": "common",
            "created_at": "2026-05-24T06:25:09Z",
        }

        with patch.dict(sys.modules, {"psycopg": fake_psycopg}), \
             patch.object(portal_app, "litellm_psycopg_database_url", return_value="postgresql://example/db"):
            portal_app._litellm_key_totals_for_entries_uncached("u@example.com", [entry])

        self.assertIn("created_at_utc", captured["sql"])
        self.assertIn("d.date >= (n.created_at_utc::date)::text", captured["sql"])
        self.assertIn('s."endTime" >= n.created_at_utc', captured["sql"])
        self.assertIn("2026-05-24 06:25:09.000000", captured["sql"])


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

    def test_litellm_key_total_for_matches_alias_variants(self):
        row = {
            "key_id": "claude:prod-key",
            "key_label": "claude:prod-key",
            "api_key": "hashed-token",
            "total_requests": 7,
        }
        index = portal_app.index_litellm_key_totals([row])

        matched = portal_app.litellm_key_total_for(index, "sk-prod", {"label": "prod-key", "model_group": "claude"})

        self.assertIs(matched, row)

    def test_my_keys_returns_litellm_totals_matched_by_alias_and_hash(self):
        user_data = {
            "users": {"u@example.com": {"name": "User", "api_keys": ["sk-raw", "sk-hash", "sk-unused"]}},
            "keys": {
                "sk-raw": {"email": "u@example.com", "label": "raw-label", "model_group": "common", "source": "cliproxy"},
                "sk-hash": {"email": "u@example.com", "label": "hash-label", "model_group": "claude", "source": "cliproxy"},
                "sk-unused": {"email": "u@example.com", "label": "unused", "model_group": "common", "source": "cliproxy"},
            },
        }
        rows = [
            {
                "key_id": "common:raw-label",
                "key_label": "common:raw-label",
                "api_key": "alias-token",
                "total_requests": 3,
                "total_tokens": 30,
                "input_tokens": 10,
                "output_tokens": 20,
                "cached_tokens": 4,
                "reasoning_tokens": 2,
                "spend_usd": 0.123456,
            },
            {
                "key_id": "hashed-row",
                "key_label": "hashed-row",
                "api_key": portal_app._litellm_token_hash("sk-hash"),
                "total_requests": 5,
                "total_tokens": 50,
                "input_tokens": 15,
                "output_tokens": 35,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "spend_usd": 0.5,
            },
        ]
        batch_rows = {
            "sk-raw": rows[0],
            "sk-hash": rows[1],
        }
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@example.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=user_data), \
             patch.object(portal_app, "litellm_key_totals_for_entries", return_value=batch_rows), \
             patch.object(portal_app, "litellm_user_key_totals", return_value=[]):
            response = client.post("/api/my-keys", json={})

        self.assertEqual(response.status_code, 200)
        by_key = {item["key"]: item for item in response.get_json()["keys"]}
        self.assertEqual(by_key["sk-raw"]["total_requests"], 3)
        self.assertEqual(by_key["sk-raw"]["total_tokens"], 30)
        self.assertEqual(by_key["sk-raw"]["token_breakdown"]["cached_tokens"], 4)
        self.assertEqual(by_key["sk-raw"]["estimated_cost_usd"], 0.1235)
        self.assertEqual(by_key["sk-hash"]["total_requests"], 5)
        self.assertEqual(by_key["sk-hash"]["total_tokens"], 50)
        self.assertEqual(by_key["sk-unused"]["total_requests"], 0)
        self.assertEqual(by_key["sk-unused"]["total_tokens"], 0)

    def test_my_keys_returns_batched_total_today_and_last_used_for_index_only_key(self):
        user_data = {
            "users": {"u@example.com": {"name": "User", "api_keys": []}},
            "keys": {"usr_pool_0181_x660001": {"email": "u@example.com", "label": "biao", "model_group": "common", "source": "cliproxy"}},
        }
        batched = {"usr_pool_0181_x660001": {
            "total_requests": 28,
            "total_tokens": 2193870,
            "input_tokens": 2177802,
            "output_tokens": 16068,
            "cached_tokens": 548864,
            "reasoning_tokens": 2157,
            "spend_usd": 8.9,
            "today_requests": 4,
            "today_tokens": 2326372,
            "today_input_tokens": 2309217,
            "today_output_tokens": 17155,
            "today_cached_tokens": 571392,
            "today_reasoning_tokens": 2157,
            "today_spend_usd": 9.49,
            "last_used_at": "2026-05-26T08:00:00Z",
        }}
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@example.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=user_data), \
             patch.object(portal_app, "litellm_key_totals_for_entries", return_value=batched), \
             patch.object(portal_app, "litellm_user_key_totals", return_value=[]):
            response = client.post("/api/my-keys", json={})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["usage_scope"], "total_and_today")
        self.assertEqual(payload["keys"][0]["key"], "usr_pool_0181_x660001")
        self.assertEqual(payload["keys"][0]["total_requests"], 28)
        self.assertEqual(payload["keys"][0]["total_tokens"], 2193870)
        self.assertEqual(payload["keys"][0]["today_tokens"], 2326372)
        self.assertEqual(payload["keys"][0]["last_used_at"], "2026-05-26T08:00:00Z")

    def test_my_keys_includes_identity_usage_key_not_registered_in_portal(self):
        user_data = {
            "users": {"u@example.com": {"name": "User", "api_keys": ["sk-portal"]}},
            "keys": {"sk-portal": {"email": "u@example.com", "label": "portal", "model_group": "common"}},
        }
        batched = {"sk-portal": {"total_requests": 0, "total_tokens": 0}}
        identity_rows = [{
            "api_key": "hashed-token",
            "key_id": "common:usr_pool_0181_d2ba19660001",
            "key_label": "common:usr_pool_0181_d2ba19660001",
            "total_requests": 86,
            "total_tokens": 8820992,
            "input_tokens": 8800000,
            "output_tokens": 20992,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "spend_usd": 1.2,
            "today_requests": 86,
            "today_tokens": 8820992,
            "last_used_at": "2026-06-02T16:24:24Z",
        }]
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@example.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=user_data), \
             patch.object(portal_app, "litellm_key_totals_for_entries", return_value=batched), \
             patch.object(portal_app, "litellm_user_key_totals", return_value=identity_rows):
            response = client.post("/api/my-keys", json={})

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        by_key = {item["key"]: item for item in payload["keys"]}
        self.assertIn("common:usr_pool_0181_d2ba19660001", by_key)
        pool_key = by_key["common:usr_pool_0181_d2ba19660001"]
        self.assertEqual(pool_key["today_requests"], 86)
        self.assertEqual(pool_key["today_tokens"], 8820992)
        self.assertFalse(pool_key["can_revoke"])
        self.assertTrue(pool_key["synthetic_usage_key"])
        self.assertEqual(payload["user_today_tokens"], 8820992)

    def test_user_keys_returns_litellm_totals_matched_by_alias_and_keeps_unmatched_zero(self):
        user_data = {
            "users": {"u@example.com": {"name": "User", "api_keys": ["sk-alias", "sk-unused"]}},
            "keys": {
                "sk-alias": {"email": "u@example.com", "label": "team-key", "model_group": "deepseek", "source": "cliproxy"},
                "sk-unused": {"email": "u@example.com", "label": "unused", "model_group": "common", "source": "cliproxy"},
            },
        }
        rows = [{
            "key_id": "deepseek:team-key",
            "key_label": "deepseek:team-key",
            "api_key": "stored-token",
            "total_requests": 11,
            "success_count": 10,
            "failure_count": 1,
            "total_tokens": 110,
            "input_tokens": 40,
            "output_tokens": 70,
            "cached_tokens": 6,
            "reasoning_tokens": 3,
            "spend_usd": 1.23456,
        }]
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@example.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=user_data), \
             patch.object(portal_app, "litellm_key_totals_for_entries", return_value={"sk-alias": rows[0]}), \
             patch.object(portal_app, "litellm_spend_pricing_metadata", return_value={}):
            response = client.get("/api/user-keys?email=u@example.com")

        self.assertEqual(response.status_code, 200)
        by_key = {item["key"]: item for item in response.get_json()["keys"]}
        self.assertEqual(by_key["sk-alias"]["total_requests"], 11)
        self.assertEqual(by_key["sk-alias"]["total_tokens"], 110)
        self.assertEqual(by_key["sk-alias"]["token_breakdown"]["reasoning_tokens"], 3)
        self.assertEqual(by_key["sk-alias"]["estimated_cost_usd"], 1.2346)
        self.assertEqual(by_key["sk-unused"]["total_requests"], 0)
        self.assertEqual(by_key["sk-unused"]["total_tokens"], 0)

    def test_user_key_timeseries_accepts_key_listed_under_requested_admin_user(self):
        user_data = {
            "users": {"owner@example.com": {"name": "Owner", "api_keys": ["sk-shared"]}},
            "keys": {"sk-shared": {"email": "stale@example.com", "label": "shared", "model_group": "common"}},
        }
        rows = [{
            "date": "2026-05-26",
            "hour": "2026-05-26 10:00",
            "requests": 2,
            "success_count": 1,
            "failure_count": 1,
            "total_tokens": 100,
            "input_tokens": 40,
            "output_tokens": 60,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
            "spend_usd": 0.25,
        }]
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "biao.chen@zilliz.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=user_data), \
             patch.object(portal_app, "litellm_key_timeseries", return_value=rows), \
             patch.object(portal_app, "litellm_spend_pricing_metadata", return_value={}):
            response = client.get("/api/user-key-timeseries?email=owner@example.com&api_key=sk-shared&date=2026-05-26")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["email"], "owner@example.com")
        self.assertEqual(payload["totals"]["requests"], 2)
        self.assertEqual(payload["totals"]["total_tokens"], 100)


if __name__ == "__main__":
    unittest.main()
