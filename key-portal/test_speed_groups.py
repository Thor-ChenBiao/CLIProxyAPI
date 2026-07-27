import unittest
from pathlib import Path
from unittest.mock import patch

import app as portal_app
import approval
import fast_mode
import speed_groups


class SpeedGroupDomainTests(unittest.TestCase):
    def test_missing_group_defaults_to_standard(self):
        self.assertEqual(speed_groups.normalize_speed_group(None), "standard")
        self.assertEqual(speed_groups.normalize_speed_group(""), "standard")

    def test_known_groups_are_normalized(self):
        self.assertEqual(speed_groups.normalize_speed_group(" STANDARD "), "standard")
        self.assertEqual(speed_groups.normalize_speed_group("FAST"), "fast")

    def test_invalid_group_is_rejected_for_writes(self):
        with self.assertRaises(ValueError):
            speed_groups.require_speed_group("turbo")

    def test_usr_pool_key_uses_litellm_canonical_form(self):
        self.assertEqual(
            speed_groups.canonical_litellm_key("usr_pool_0174_x"),
            "sk-usr_pool_0174_x",
        )

    def test_sk_key_keeps_its_canonical_form(self):
        self.assertEqual(
            speed_groups.canonical_litellm_key("sk-existing"),
            "sk-existing",
        )

    def test_metadata_merge_preserves_existing_fields(self):
        metadata = {
            "email": "u@zilliz.com",
            "model_group": "common",
        }

        self.assertEqual(
            speed_groups.merge_speed_group_metadata(metadata, "fast"),
            {
                "email": "u@zilliz.com",
                "model_group": "common",
                "speed_group": "fast",
            },
        )

    def test_invalid_metadata_is_read_as_standard(self):
        self.assertEqual(
            speed_groups.effective_speed_group({"speed_group": "turbo"}),
            "standard",
        )


class SpeedGroupApiTests(unittest.TestCase):
    def setUp(self):
        self.user_data = {
            "users": {
                "u@zilliz.com": {"api_keys": ["usr_pool_test"]},
                "other@zilliz.com": {"api_keys": ["sk-other"]},
            },
            "keys": {
                "usr_pool_test": {
                    "email": "u@zilliz.com",
                    "source": "cliproxy",
                },
                "sk-other": {
                    "email": "other@zilliz.com",
                    "source": "litellm",
                },
            },
        }

    def portal_session(self):
        return patch.object(
            portal_app,
            "current_portal_session",
            return_value={"email": "u@zilliz.com", "user": {}},
        )

    def test_owner_can_switch_key_to_fast(self):
        speed_info = {
            "usr_pool_test": {
                "editable": True,
                "metadata": {
                    "email": "u@zilliz.com",
                    "model_group": "common",
                },
                "speed_group": "standard",
            },
        }
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data), \
             patch.object(
                 portal_app,
                 "litellm_speed_groups_for_entries",
                 return_value=speed_info,
             ), \
             patch.object(
                 portal_app,
                 "update_litellm_key_metadata",
                 return_value=(True, None),
             ) as update:
            response = client.post(
                "/api/keys/speed-group",
                json={"key": "usr_pool_test", "speed_group": "fast"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["speed_group"], "fast")
        update.assert_called_once_with(
            "sk-usr_pool_test",
            {
                "email": "u@zilliz.com",
                "model_group": "common",
                "speed_group": "fast",
            },
        )

    def test_non_owner_cannot_switch_key(self):
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data), \
             patch.object(portal_app, "update_litellm_key_metadata") as update:
            response = client.post(
                "/api/keys/speed-group",
                json={"key": "sk-other", "speed_group": "fast"},
            )

        self.assertEqual(response.status_code, 403)
        update.assert_not_called()

    def test_invalid_group_returns_400(self):
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data):
            response = client.post(
                "/api/keys/speed-group",
                json={"key": "usr_pool_test", "speed_group": "turbo"},
            )

        self.assertEqual(response.status_code, 400)

    def test_unmatched_key_returns_502(self):
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data), \
             patch.object(
                 portal_app,
                 "litellm_speed_groups_for_entries",
                 return_value={
                     "usr_pool_test": {
                         "editable": False,
                         "metadata": {},
                         "speed_group": "standard",
                     },
                 },
             ), \
             patch.object(portal_app, "update_litellm_key_metadata") as update:
            response = client.post(
                "/api/keys/speed-group",
                json={"key": "usr_pool_test", "speed_group": "fast"},
            )

        self.assertEqual(response.status_code, 502)
        update.assert_not_called()

    def test_my_keys_returns_authoritative_speed_group(self):
        speed_info = {
            "usr_pool_test": {
                "editable": True,
                "metadata": {"speed_group": "fast"},
                "speed_group": "fast",
            },
        }
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data), \
             patch.object(portal_app, "litellm_key_totals_for_entries", return_value={}), \
             patch.object(
                 portal_app,
                 "merge_litellm_identity_usage_keys",
                 side_effect=lambda email, entries, totals: (entries, totals),
             ), \
             patch.object(
                 portal_app,
                 "litellm_speed_groups_for_entries",
                 return_value=speed_info,
             ), \
             patch.object(portal_app, "_key_budget", return_value=None):
            response = client.post("/api/my-keys", json={})

        self.assertEqual(response.status_code, 200)
        key_info = response.get_json()["keys"][0]
        self.assertEqual(key_info["speed_group"], "fast")
        self.assertTrue(key_info["can_change_speed"])

    def test_my_keys_deferred_usage_skips_expensive_queries(self):
        speed_info = {
            "usr_pool_test": {
                "editable": True,
                "metadata": {"speed_group": "fast"},
                "speed_group": "fast",
            },
        }
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data), \
             patch.object(portal_app, "litellm_key_totals_for_entries") as totals, \
             patch.object(portal_app, "merge_litellm_identity_usage_keys") as merge, \
             patch.object(
                 portal_app,
                 "litellm_speed_groups_for_entries",
                 return_value=speed_info,
             ), \
             patch.object(portal_app, "_key_budget", return_value=None):
            response = client.post(
                "/api/my-keys",
                json={"defer_usage": True},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.get_json()
        self.assertTrue(payload["usage_pending"])
        self.assertEqual(payload["keys"][0]["speed_group"], "fast")
        self.assertTrue(payload["keys"][0]["usage_pending"])
        totals.assert_not_called()
        merge.assert_not_called()

    def test_query_by_key_returns_authoritative_speed_group(self):
        speed_info = {
            "usr_pool_test": {
                "editable": True,
                "metadata": {"speed_group": "fast"},
                "speed_group": "fast",
            },
        }
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data), \
             patch.object(portal_app, "litellm_user_key_totals", return_value=[]), \
             patch.object(portal_app, "litellm_key_total", return_value={}), \
             patch.object(
                 portal_app,
                 "litellm_speed_groups_for_entries",
                 return_value=speed_info,
             ), \
             patch.object(portal_app, "_key_budget", return_value=None):
            response = client.post(
                "/api/query-by-key",
                json={"api_key": "usr_pool_test"},
            )

        self.assertEqual(response.status_code, 200)
        key_info = response.get_json()["all_keys"][0]
        self.assertEqual(key_info["speed_group"], "fast")
        self.assertTrue(key_info["can_change_speed"])

    def test_admin_user_keys_returns_speed_group(self):
        speed_info = {
            "usr_pool_test": {
                "editable": True,
                "metadata": {"speed_group": "fast"},
                "speed_group": "fast",
            },
        }
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data), \
             patch.object(portal_app, "litellm_key_totals_for_entries", return_value={}), \
             patch.object(
                 portal_app,
                 "litellm_speed_groups_for_entries",
                 return_value=speed_info,
             ) as speed_lookup, \
             patch.object(portal_app, "_key_budget", return_value=None):
            response = client.get("/api/user-keys?email=u@zilliz.com")

        self.assertEqual(response.status_code, 200)
        key_info = response.get_json()["keys"][0]
        self.assertEqual(key_info["speed_group"], "fast")
        self.assertTrue(key_info["can_change_speed"])
        speed_lookup.assert_called_once()

    def test_user_stats_speed_groups_are_batched(self):
        stats = [
            {
                "email": "u@zilliz.com",
                "key_count": 2,
                "_api_keys": ["sk-standard", "sk-fast"],
            },
            {
                "email": "other@zilliz.com",
                "key_count": 1,
                "keys": [{"key": "sk-other-fast"}],
            },
        ]
        speed_info = {
            "sk-standard": {"editable": True, "speed_group": "standard"},
            "sk-fast": {"editable": True, "speed_group": "fast"},
            "sk-other-fast": {"editable": True, "speed_group": "fast"},
        }
        with patch.object(
            portal_app,
            "litellm_speed_groups_for_entries",
            return_value=speed_info,
        ) as speed_lookup:
            portal_app.annotate_user_stats_speed_groups(stats)

        self.assertEqual(stats[0]["fast_key_count"], 1)
        self.assertEqual(stats[1]["fast_key_count"], 1)
        self.assertEqual(stats[1]["keys"][0]["speed_group"], "fast")
        speed_lookup.assert_called_once()


class SpeedGroupCreationTests(unittest.TestCase):
    def portal_session(self):
        return patch.object(
            portal_app,
            "current_portal_session",
            return_value={
                "email": "u@zilliz.com",
                "user": {"name": "User"},
            },
        )

    def test_direct_registration_forwards_fast(self):
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(
                 portal_app,
                 "assign_key_to_user",
                 return_value=("sk-created", None),
             ) as assign:
            response = client.post(
                "/api/register-key",
                json={
                    "label": "interactive",
                    "model_group": "common",
                    "speed_group": "fast",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["speed_group"], "fast")
        self.assertEqual(assign.call_args.kwargs["speed_group"], "fast")


    def test_registration_defaults_to_standard(self):
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(
                 portal_app,
                 "assign_key_to_user",
                 return_value=("sk-created", None),
             ) as assign:
            response = client.post(
                "/api/register-key",
                json={"label": "offline", "model_group": "common"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["speed_group"], "standard")
        self.assertEqual(assign.call_args.kwargs["speed_group"], "standard")

    def test_approval_registration_forwards_fast(self):
        with portal_app.app.test_client() as client, \
             self.portal_session(), \
             patch.object(
                 approval,
                 "create_approval_request",
                 return_value=("approval-id", None),
             ) as create:
            response = client.post(
                "/api/register-key",
                json={
                    "label": "interactive",
                    "model_group": "claude",
                    "speed_group": "fast",
                    "reason": "interactive work",
                    "daily_budget": "5",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["pending_approval"])
        self.assertEqual(create.call_args.kwargs["speed_group"], "fast")

    def test_approved_request_preserves_fast(self):
        row = {
            "id": 1,
            "email": "u@zilliz.com",
            "name": "User",
            "label": "interactive",
            "model_group": "claude",
            "daily_budget": "5",
            "request_payload": '{"speed_group":"fast"}',
        }
        with patch.object(
            portal_app,
            "assign_key_to_user",
            return_value=("sk-created", None),
        ) as assign, \
             patch.object(approval, "_mark_request_approved", return_value=1), \
             patch.object(approval, "_notify_user_approved"):
            success, _ = approval._on_new_key_approved(row)

        self.assertTrue(success)
        self.assertEqual(assign.call_args.kwargs["speed_group"], "fast")


class GlobalFastModeApiTests(unittest.TestCase):
    def session(self, email):
        return patch.object(
            portal_app,
            "current_portal_session",
            return_value={"email": email, "user": {"email": email}},
        )

    def test_admin_can_get_global_state(self):
        state = {
            "enabled": True,
            "available": True,
            "source": "redis",
            "updated_at": "",
            "updated_by": "",
            "reason": "",
        }
        with portal_app.app.test_client() as client, \
             self.session("biao.chen@zilliz.com"), \
             patch.object(portal_app.global_fast_mode_store, "load", return_value=state):
            response = client.get("/api/auth-stats/fast-mode")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["enabled"])

    def test_non_admin_cannot_get_global_state(self):
        with portal_app.app.test_client() as client, \
             self.session("u@zilliz.com"), \
             patch.object(portal_app.global_fast_mode_store, "load") as load:
            response = client.get("/api/auth-stats/fast-mode")

        self.assertEqual(response.status_code, 403)
        load.assert_not_called()

    def test_admin_can_update_global_state(self):
        saved = {
            "enabled": False,
            "available": True,
            "source": "redis",
            "updated_at": "2026-07-24T00:00:00+00:00",
            "updated_by": "biao.chen@zilliz.com",
            "reason": "auth-stats global Fast toggle",
        }
        with portal_app.app.test_client() as client, \
             self.session("biao.chen@zilliz.com"), \
             patch.object(portal_app.global_fast_mode_store, "save", return_value=saved) as save:
            response = client.post(
                "/api/auth-stats/fast-mode",
                json={"enabled": False},
            )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.get_json()["enabled"])
        save.assert_called_once_with(
            False,
            updated_by="biao.chen@zilliz.com",
            reason="auth-stats global Fast toggle",
        )

    def test_invalid_global_state_update_returns_400(self):
        with portal_app.app.test_client() as client, \
             self.session("biao.chen@zilliz.com"), \
             patch.object(portal_app.global_fast_mode_store, "save") as save:
            response = client.post(
                "/api/auth-stats/fast-mode",
                json={"enabled": "false"},
            )

        self.assertEqual(response.status_code, 400)
        save.assert_not_called()

    def test_global_state_store_failure_returns_503(self):
        with portal_app.app.test_client() as client, \
             self.session("biao.chen@zilliz.com"), \
             patch.object(
                 portal_app.global_fast_mode_store,
                 "load",
                 side_effect=fast_mode.FastModeUnavailable("Redis unavailable"),
             ):
            response = client.get("/api/auth-stats/fast-mode")

        self.assertEqual(response.status_code, 503)


class SpeedGroupTemplateTests(unittest.TestCase):
    def test_application_form_defaults_to_standard(self):
        html = (
            Path(__file__).parent / "templates" / "index.html"
        ).read_text(encoding="utf-8")

        self.assertIn('value="standard" checked', html)
        self.assertIn('value="fast"', html)
        self.assertIn(
            "离线任务、批处理和后台任务要求使用普通模式",
            html,
        )
        self.assertIn("/static/key_speed_groups.js", html)

    def test_my_keys_loads_speed_controls(self):
        html = (
            Path(__file__).parent / "templates" / "my_keys.html"
        ).read_text(encoding="utf-8")

        self.assertIn("/static/key_speed_groups.js", html)
        self.assertIn("运行模式", html)
        self.assertIn("setKeySpeedGroup", html)

    def test_my_keys_loads_keys_before_usage(self):
        html = (
            Path(__file__).parent / "templates" / "my_keys.html"
        ).read_text(encoding="utf-8")

        self.assertIn("defer_usage: true", html)
        self.assertIn("usagePending", html)

    def test_admin_users_shows_fast_key_counts_and_rows(self):
        html = (
            Path(__file__).parent / "templates" / "admin_users.html"
        ).read_text(encoding="utf-8")

        self.assertIn("fast_key_count", html)
        self.assertIn("<th>运行模式</th>", html)
        self.assertIn("apiKey.speed_group", html)

    def test_auth_stats_has_global_fast_control(self):
        html = (
            Path(__file__).parent / "templates" / "admin_auth_stats.html"
        ).read_text(encoding="utf-8")

        self.assertIn('id="fastModeToggle"', html)
        self.assertIn("/api/auth-stats/fast-mode", html)
        self.assertIn("所有 Key 强制 default；原有 Fast 分组已保留", html)

if __name__ == "__main__":
    unittest.main()
