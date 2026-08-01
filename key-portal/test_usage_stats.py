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

    def test_sonnet5_alias_is_available_only_to_gpt_group(self):
        self.assertIn("claude-sonnet-5", portal_app.LITELLM_MODEL_GROUPS["common"]["models"])
        self.assertNotIn("claude-sonnet-5", portal_app.LITELLM_MODEL_GROUPS["claude"]["models"])


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
    @staticmethod
    def usage_bucket(requests):
        return {
            "requests": requests,
            "success": requests,
            "failure": 0,
            "tokens": requests * 100,
            "input_tokens": requests * 80,
            "output_tokens": requests * 20,
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        }

    def test_auth_stats_reads_distinct_windows_from_persisted_snapshot(self):
        files = [{
            "node": "node-a",
            "account": "account@example.com",
            "auth_index": "auth-a",
            "provider": "codex",
        }]
        row = {
            "node": "node-a",
            "source": "account@example.com",
            "auth_index": "auth-a",
            "last_1h": self.usage_bucket(1),
            "last_5h": self.usage_bucket(2),
            "last_24h": self.usage_bucket(3),
            "last_7d": self.usage_bucket(4),
            "total": self.usage_bucket(4),
            "today": self.usage_bucket(3),
            "history": {},
        }
        service = portal_app.auth_stats_service.AuthStatsService(
            portal_state=object(),
            nodes=[{"name": "node-a", "url": "http://node-a"}],
            call_management_api_node=lambda *args, **kwargs: ({}, None),
            get_cluster_usage=lambda: self.fail("persisted stats must not drain the live usage queue"),
            get_cluster_auth_files=lambda: (files, []),
            usage_summary_loader=lambda: ({"today_tokens": 300}, None),
            parse_detail_time=portal_app.parse_detail_time,
            parse_detail_time_utc=portal_app.parse_detail_time_utc,
            build_token_breakdown=portal_app.build_token_breakdown,
            usage_snapshot_loader=lambda: {
                "auth_files": [row],
                "errors": [],
                "coverage_seconds": 8 * 24 * 3600,
            },
        )

        node = service.build()["nodes"]["node-a"]

        self.assertEqual(node["last_1h"]["requests"], 1)
        self.assertEqual(node["last_5h"]["requests"], 2)
        self.assertEqual(node["last_24h"]["requests"], 3)
        self.assertEqual(node["last_7d"]["requests"], 4)

    def test_seven_day_window_includes_three_day_old_usage(self):
        now = datetime.utcnow()
        details = []
        for index, age in enumerate((timedelta(minutes=30), timedelta(hours=2), timedelta(hours=10), timedelta(days=3))):
            details.append({
                "node": "node-a",
                "timestamp": (now - age).strftime("%Y-%m-%dT%H:%M:%SZ"),
                "source": "account@example.com",
                "auth_index": "auth-a",
                "tokens": {"total_tokens": (index + 1) * 100},
                "failed": False,
            })
        usage_payload = {
            "usage": {
                "apis": {
                    "k1": {"models": {"m": {"details": details}}},
                },
            },
        }
        files = [{
            "node": "node-a",
            "account": "account@example.com",
            "auth_index": "auth-a",
            "provider": "codex",
        }]
        service = portal_app.auth_stats_service.AuthStatsService(
            portal_state=object(),
            nodes=[{"name": "node-a", "url": "http://node-a"}],
            call_management_api_node=lambda *args, **kwargs: ({}, None),
            get_cluster_usage=lambda: usage_payload,
            get_cluster_auth_files=lambda: (files, []),
            usage_summary_loader=lambda: ({}, None),
            parse_detail_time=portal_app.parse_detail_time,
            parse_detail_time_utc=portal_app.parse_detail_time_utc,
            build_token_breakdown=portal_app.build_token_breakdown,
        )

        node = service.build()["nodes"]["node-a"]

        self.assertEqual(node["last_1h"]["requests"], 1)
        self.assertEqual(node["last_5h"]["requests"], 2)
        self.assertEqual(node["last_24h"]["requests"], 3)
        self.assertEqual(node["last_7d"]["requests"], 4)

    def test_auth_stats_template_marks_incomplete_windows(self):
        template_path = os.path.join(os.path.dirname(__file__), "templates", "admin_auth_stats.html")
        with open(template_path, "r", encoding="utf-8") as template_file:
            template = template_file.read()

        self.assertIn("数据积累中", template)
        self.assertIn("coverage_seconds", template)
        self.assertIn("当前原生额度占用", template)
        self.assertIn("native_usage_percent", template)
        self.assertIn("Token 容量辅助估算", template)

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

    def test_codex_primary_window_is_classified_by_duration(self):
        service = self.auth_stats_service()
        quota = service.quota_from_snapshot({
            "provider": "codex",
            "quota_snapshot": {
                "type": "codex",
                "payload": {
                    "plan_type": "pro",
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 62,
                            "limit_window_seconds": 7 * 24 * 3600,
                            "reset_after_seconds": 3600,
                        },
                    },
                },
            },
        })

        self.assertIsNone(quota["windows"]["last_5h"])
        self.assertEqual(quota["windows"]["last_7d"]["used_percent"], 62)
        self.assertEqual(quota["windows"]["last_7d"]["limit_window_seconds"], 7 * 24 * 3600)

    def test_codex_primary_and_secondary_windows_keep_actual_durations(self):
        service = self.auth_stats_service()
        quota = service.quota_from_snapshot({
            "provider": "codex",
            "quota_snapshot": {
                "type": "codex",
                "payload": {
                    "rate_limit": {
                        "primary_window": {
                            "used_percent": 20,
                            "limit_window_seconds": 5 * 3600,
                        },
                        "secondary_window": {
                            "used_percent": 40,
                            "limit_window_seconds": 7 * 24 * 3600,
                        },
                    },
                },
            },
        })

        self.assertEqual(quota["windows"]["last_5h"]["used_percent"], 20)
        self.assertEqual(quota["windows"]["last_7d"]["used_percent"], 40)

    def test_native_quota_usage_does_not_require_local_token_history(self):
        service = self.auth_stats_service()
        stats = {}
        for index in range(10):
            stats[str(index)] = {
                "today": {"tokens": 0},
                "disabled": False,
                "unavailable": False,
                "quota_window": {"tokens": 0},
                "quota": {
                    "windows": {
                        "last_7d": {
                            "used_percent": 60 + (index % 3),
                            "limit_window_seconds": 7 * 24 * 3600,
                            "reset_at": "2026-08-05T04:00:00Z",
                        },
                    },
                },
            }
        stats["disabled-a"] = {
            "today": {"tokens": 0},
            "disabled": True,
            "unavailable": False,
            "quota_window": {"tokens": 0},
            "quota": {"windows": {}},
        }

        result = service.build_today_quota_usage(stats, "2026-08-01", 1000)

        self.assertTrue(result["native_quota_configured"])
        self.assertEqual(result["native_inferred_account_count"], 10)
        self.assertEqual(result["native_usage_percent"], 60.9)
        self.assertEqual(result["native_remaining_percent"], 39.1)
        self.assertEqual(result["account_count"], 10)
        self.assertEqual(result["total_account_count"], 11)
        self.assertEqual(result["disabled_account_count"], 1)
        self.assertFalse(result["configured"])

    def test_aligned_quota_usage_enables_capacity_inference(self):
        loader_requests = []

        def load_quota_usage(requests):
            loader_requests.extend(requests)
            return {
                "node-a|auth-a": {
                    "node": "node-a",
                    "auth_index": "auth-a",
                    "start_at": "2026-07-29T04:00:00Z",
                    "end_at": "2026-08-01T00:00:00Z",
                    "requests": 12,
                    "tokens": 620_000,
                    "coverage_seconds": 244800,
                    "required_seconds": 244800,
                    "complete": True,
                },
            }

        service = portal_app.auth_stats_service.AuthStatsService(
            portal_state=object(),
            nodes=[],
            call_management_api_node=lambda *args, **kwargs: ({}, None),
            get_cluster_usage=lambda: {},
            get_cluster_auth_files=lambda: ([], []),
            usage_summary_loader=lambda: ({}, None),
            parse_detail_time=portal_app.parse_detail_time,
            parse_detail_time_utc=portal_app.parse_detail_time_utc,
            build_token_breakdown=portal_app.build_token_breakdown,
            quota_usage_loader=load_quota_usage,
        )
        stats = {
            "node-a:auth-a": {
                "node": "node-a",
                "auth_index": "auth-a",
                "disabled": False,
                "unavailable": False,
                "today": {"tokens": 1000},
                "_quota_usage_details": [],
                "_quota_usage_windows": {},
                "quota": {
                    "windows": {
                        "last_7d": {
                            "used_percent": 62,
                            "limit_window_seconds": 7 * 24 * 3600,
                            "reset_at": "2026-08-05T04:00:00Z",
                        },
                    },
                },
            },
        }

        self.assertEqual(service.attach_aligned_quota_usage(stats), [])
        service.apply_quota_window_usage(stats)
        result = service.build_today_quota_usage(stats, "2026-08-01", 1000)

        self.assertEqual(len(loader_requests), 1)
        self.assertTrue(stats["node-a:auth-a"]["quota_window"]["complete"])
        self.assertEqual(stats["node-a:auth-a"]["quota_window"]["tokens"], 620_000)
        self.assertTrue(result["configured"])
        self.assertEqual(result["single_account_window_token_limit"], 1_000_000)
        self.assertEqual(result["inferred_account_count"], 1)

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
    def test_user_key_stats_for_date_uses_daily_user_spend_identity(self):
        captured = []

        def fake_psql_json(sql, timeout=15):
            captured.append(sql)
            return []

        with patch.object(portal_app, "litellm_psql_json", side_effect=fake_psql_json), \
             patch.object(portal_app, "beijing_today", return_value="2026-07-09"):
            portal_app.litellm_user_key_stats_for_date("u@example.com", "2026-07-08")

        self.assertIn('"LiteLLM_DailyUserSpend"', captured[0])
        self.assertNotIn('"LiteLLM_SpendLogs"', captured[0])
        self.assertIn("d.date = '2026-07-08'", captured[0])
        self.assertIn("v.metadata->>'email'", captured[0])
        self.assertIn("d.user_id", captured[0])

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

    def test_key_totals_resolve_pool_key_through_verification_alias(self):
        captured = {}

        class FakeCursor:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def execute(self, sql):
                if '"LiteLLM_DailyUserSpend"' in sql:
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
            "key": "usr_pool_0206_549dd675ffe9",
            "label": "buqian",
            "model_group": "common",
        }

        with patch.dict(sys.modules, {"psycopg": fake_psycopg}), \
             patch.object(portal_app, "litellm_psycopg_database_url", return_value="postgresql://example/db"):
            portal_app._litellm_key_totals_for_entries_uncached("buqian.zheng@zilliz.com", [entry])

        sql = captured["sql"]
        self.assertIn("matched_alias_tokens AS MATERIALIZED", sql)
        self.assertIn("v.key_alias = n.raw_key", sql)
        self.assertIn("':' || n.raw_key", sql)
        self.assertIn("SELECT raw_key, api_key, created_at_utc FROM matched_alias_tokens", sql)

    def test_user_usage_summary_includes_alias_resolved_key_totals(self):
        key_totals = {
            "usr_pool_0206_549dd675ffe9": {
                "total_requests": 75_116,
                "success_count": 75_100,
                "failure_count": 16,
                "total_tokens": 9_656_467_901,
                "input_tokens": 9_600_000_000,
                "output_tokens": 56_467_901,
                "cached_tokens": 9_100_000_000,
                "reasoning_tokens": 0,
                "spend_usd": 12_345.67,
            },
        }
        today = {
            "today_requests": 884,
            "today_tokens": 125_550_618,
        }

        with patch.object(portal_app, "litellm_key_totals_for_entries", return_value=key_totals), \
             patch.object(portal_app, "litellm_user_today_usage_summary", return_value=today), \
             patch.object(portal_app, "beijing_today", return_value="2026-07-23"):
            summary = portal_app.litellm_user_usage_summary(
                "buqian.zheng@zilliz.com",
                ["usr_pool_0206_549dd675ffe9"],
            )

        self.assertEqual(summary["total_requests"], 75_116)
        self.assertEqual(summary["total_tokens"], 9_656_467_901)
        self.assertEqual(summary["today_requests"], 884)
        self.assertEqual(summary["today_tokens"], 125_550_618)


class UsageSummaryQueryTests(unittest.TestCase):
    def setUp(self):
        self.original_cache = dict(portal_app._litellm_usage_cache)
        portal_app._litellm_usage_cache["data"] = None
        portal_app._litellm_usage_cache["last_update"] = 0

    def tearDown(self):
        portal_app._litellm_usage_cache.clear()
        portal_app._litellm_usage_cache.update(self.original_cache)

    def test_missing_today_query_does_not_cache_fabricated_zeroes(self):
        cumulative = {
            "total_requests": 100,
            "success_count": 99,
            "failure_count": 1,
            "total_tokens": 1000,
            "input_tokens": 900,
            "output_tokens": 100,
            "cached_tokens": 300,
            "reasoning_tokens": 10,
            "spend_usd": 2.5,
            "last_end_time": "2026-07-13T07:00:00Z",
        }

        with patch.object(portal_app, "litellm_database_url", return_value="postgresql://example/db"), \
             patch.object(portal_app, "query_litellm_daily_spend_aggregate", return_value=cumulative), \
             patch.object(portal_app, "query_litellm_today_spendlogs_aggregate", return_value=None):
            result = portal_app.query_litellm_spendlogs_aggregate()

        self.assertIsNone(result)
        self.assertIsNone(portal_app._litellm_usage_cache["data"])

    def test_cached_summary_reads_through_shared_snapshot(self):
        class FakeSnapshot:
            def __init__(self):
                self.calls = 0

            def get(self, loader):
                self.calls += 1
                return {"today": "2026-07-13", "today_tokens": 123}, None

        fake_snapshot = FakeSnapshot()
        portal_app._litellm_usage_cache["data"] = {"total_tokens": 1000}
        portal_app._litellm_usage_cache["last_update"] = portal_app.time.time()

        with patch.object(portal_app, "_usage_summary_snapshot", fake_snapshot, create=True), \
             patch.object(portal_app, "_build_usage_summary_fast", side_effect=AssertionError("fresh snapshot must not query PG")):
            data, error = portal_app.get_usage_summary_cached()

        self.assertIsNone(error)
        self.assertEqual(data["today_tokens"], 123)
        self.assertEqual(fake_snapshot.calls, 1)

    def test_pg_summary_failure_is_not_replaced_with_cluster_zeroes(self):
        with patch.object(portal_app, "litellm_database_url", return_value="postgresql://example/db"), \
             patch.object(portal_app, "query_litellm_today_spendlogs_aggregate", return_value=None), \
             patch.object(portal_app, "query_litellm_spendlogs_aggregate", return_value=None), \
             patch.object(portal_app, "get_cluster_usage_summary", return_value={"usage": {}}):
            with self.assertRaisesRegex(RuntimeError, "today aggregate unavailable"):
                portal_app._build_usage_summary_fast()

    def test_today_summary_uses_daily_rows_plus_beijing_boundary_segment(self):
        captured = []
        payload = {
            "today_requests": 12,
            "today_success_count": 11,
            "today_failure_count": 1,
            "today_tokens": 1200,
            "today_input_tokens": 1000,
            "today_output_tokens": 200,
            "today_cached_tokens": 700,
            "today_reasoning_tokens": 0,
            "today_spend_usd": 3.5,
            "today_last_end_time": "2026-07-13T08:00:00Z",
        }

        def fake_psql(sql, timeout=15):
            captured.append((sql, timeout))
            return payload

        with patch.object(portal_app, "litellm_database_url", return_value="postgresql://example/db"), \
             patch.object(portal_app, "beijing_today", return_value="2026-07-13"), \
             patch.object(portal_app, "litellm_psql_json", side_effect=fake_psql), \
             patch.object(portal_app, "get_litellm_today_token_details", return_value=None, create=True):
            result = portal_app.query_litellm_today_spendlogs_aggregate()

        sql = captured[0][0]
        self.assertIn('"LiteLLM_DailyUserSpend"', sql)
        self.assertIn('"LiteLLM_SpendLogs"', sql)
        self.assertIn("use_daily", sql)
        self.assertNotIn("usage_object", sql)
        self.assertEqual(result["today_tokens"], 1200)
        self.assertEqual(result["today_cached_tokens"], 700)


class UsageSummaryTemplateTests(unittest.TestCase):
    def test_global_summary_does_not_render_error_payload_as_zeroes(self):
        template_path = os.path.join(os.path.dirname(__file__), "templates", "index.html")
        with open(template_path, "r", encoding="utf-8") as handle:
            source = handle.read()

        start = source.index("async function loadUsageSummary()")
        end = source.index("function startUsageSummaryPolling()", start)
        block = source[start:end]

        self.assertIn("if (!resp.ok || data.error)", block)
        self.assertIn("throw new Error(data.error", block)


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

    def test_user_keys_for_date_returns_daily_usage_for_hashed_portal_key(self):
        user_data = {
            "users": {"u@example.com": {"name": "User", "api_keys": ["sk-fast"]}},
            "keys": {
                "sk-fast": {
                    "email": "u@example.com",
                    "label": "team-key",
                    "model_group": "common",
                    "source": "litellm",
                },
            },
        }
        daily_rows = [{
            "api_key": portal_app._litellm_token_hash("sk-fast"),
            "key_id": "common:team-key",
            "key_label": "common:team-key",
            "total_requests": 25_007,
            "success_count": 25_000,
            "failure_count": 7,
            "total_tokens": 3_039_745_174,
            "input_tokens": 3_029_331_678,
            "output_tokens": 10_413_496,
            "cached_tokens": 2_897_521_152,
            "reasoning_tokens": 0,
            "spend_usd": 4_840.436172,
            "last_used_at": "2026-07-22T15:59:59Z",
        }]
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@example.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=user_data), \
             patch.object(portal_app, "litellm_user_key_stats_for_date", return_value=daily_rows), \
             patch.object(portal_app, "litellm_speed_groups_for_entries", return_value={"sk-fast": {"speed_group": "fast", "editable": True}}), \
             patch.object(portal_app, "litellm_spend_pricing_metadata", return_value={}):
            response = client.get("/api/user-keys?email=u@example.com&date=2026-07-22")

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertEqual(payload["source"], "litellm_daily_user_spend")
        self.assertEqual(len(payload["keys"]), 1)
        key = payload["keys"][0]
        self.assertEqual(key["key"], "sk-fast")
        self.assertEqual(key["total_requests"], 25_007)
        self.assertEqual(key["total_tokens"], 3_039_745_174)
        self.assertEqual(key["cached_tokens"], 2_897_521_152)
        self.assertEqual(key["speed_group"], "fast")

    def test_user_keys_for_date_returns_error_when_daily_usage_query_fails(self):
        user_data = {
            "users": {"u@example.com": {"name": "User", "api_keys": ["sk-test"]}},
            "keys": {"sk-test": {"email": "u@example.com", "label": "test", "model_group": "common"}},
        }
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@example.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=user_data), \
             patch.object(portal_app, "litellm_user_key_stats_for_date", return_value=None), \
             patch.object(portal_app, "litellm_speed_groups_for_entries", return_value={}):
            response = client.get("/api/user-keys?email=u@example.com&date=2026-07-22")

        self.assertEqual(response.status_code, 503)
        self.assertIn("暂时不可用", response.get_json()["error"])

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
