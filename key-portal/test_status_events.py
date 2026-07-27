import unittest

import status_events


class _Config:
    STATUS_USAGE_RECORD_LOOKBACK_DAYS = 365


class StatusEventsUsageRecordTests(unittest.TestCase):
    def test_daily_usage_record_uses_single_pass_query_with_longer_timeout(self):
        captured = {}

        def fake_psql_json(sql, timeout):
            captured["sql"] = sql
            captured["timeout"] = timeout
            return {
                "today": "2026-07-13",
                "today_tokens": 13_288_517_413,
                "previous_record_tokens": 9_494_314_512,
            }

        service = status_events.StatusEventsService(
            config=_Config(),
            portal_state=None,
            feishu=None,
            nodes=[],
            beijing_today=lambda: "2026-07-13",
            int_usage_value=lambda value: int(value or 0),
            sql_literal=lambda value: "'" + str(value).replace("'", "''") + "'",
            litellm_psql_json=fake_psql_json,
            usage_summary_loader=lambda: {},
        )

        snapshot = service.daily_usage_record_snapshot()

        self.assertEqual(snapshot["today_tokens"], 13_288_517_413)
        self.assertEqual(captured["timeout"], 60)
        self.assertNotIn("generate_series", captured["sql"])
        self.assertIn('date_trunc(\'day\', s."endTime" - interval \'12 hours\')', captured["sql"])
        self.assertIn('count(*)::bigint AS total_requests', captured["sql"])
        self.assertIn('s."endTime" >=', captured["sql"])
        self.assertIn('s."endTime" <', captured["sql"])


if __name__ == "__main__":
    unittest.main()
