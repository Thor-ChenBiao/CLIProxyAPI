"""Status page incidents and announcements for Key Portal."""

import os
from datetime import datetime, timezone


class StatusEventsService:
    def __init__(
        self,
        config,
        portal_state,
        feishu,
        database,
        nodes,
        beijing_today,
        int_usage_value,
        sql_literal,
        litellm_psql_json,
        usage_summary_loader,
        alert_mute_loader=None,
    ):
        self.config = config
        self.portal_state = portal_state
        self.feishu = feishu
        self.database = database
        self.nodes = nodes
        self.beijing_today = beijing_today
        self.int_usage_value = int_usage_value
        self.sql_literal = sql_literal
        self.litellm_psql_json = litellm_psql_json
        self.usage_summary_loader = usage_summary_loader
        self.alert_mute_loader = alert_mute_loader

    def status_page_url(self):
        if getattr(self.config, "STATUS_PUBLIC_URL", ""):
            return self.config.STATUS_PUBLIC_URL.rstrip("/")
        base = os.environ.get("PUBLIC_BASE_URL", "").strip()
        if base:
            return f"{base.rstrip('/')}/status"
        return "https://token.zasdas.com/status"

    def portal_home_url(self):
        base = os.environ.get("PUBLIC_BASE_URL", "").strip()
        if base:
            return f"{base.rstrip('/')}/"
        return "https://token.zasdas.com/"

    def format_compact_number(self, value):
        value = self.int_usage_value(value)
        if value >= 1_000_000_000:
            return f"{value / 1_000_000_000:.2f}B"
        if value >= 1_000_000:
            return f"{value / 1_000_000:.1f}M"
        if value >= 10_000:
            return f"{value / 10_000:.1f}万"
        return f"{value:,}"

    def format_event_duration(self, started_at, resolved_at=None):
        if not started_at:
            return "-"
        try:
            start = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
            end = datetime.fromisoformat(str(resolved_at).replace("Z", "+00:00")) if resolved_at else datetime.now(timezone.utc)
            seconds = max(0, int((end - start).total_seconds()))
        except Exception:
            return "-"
        if seconds < 60:
            return f"{seconds} 秒"
        minutes = seconds // 60
        if minutes < 60:
            return f"{minutes} 分钟"
        hours = minutes // 60
        return f"{hours} 小时 {minutes % 60} 分钟"

    def event_public(self, event, include_private=False):
        item = dict(event or {})
        if not include_private:
            item.pop("metadata", None)
            item.pop("dedupe_key", None)
        item["duration"] = self.format_event_duration(item.get("started_at"), item.get("resolved_at"))
        return item

    def build_payload(self, include_private=False):
        events = self.portal_state.list_status_events(120)
        active_incidents = [
            item for item in events
            if item.get("event_type") == "node_health" and item.get("status") == "open"
        ]
        recent_notices = [
            item for item in events
            if item.get("event_type") != "node_health" or item.get("status") != "open"
        ]
        affected_nodes = sorted({
            node
            for event in active_incidents
            for node in (event.get("affected_nodes") or [])
            if node
        })
        return {
            "status": "degraded" if active_incidents else "operational",
            "status_text": "部分节点异常" if active_incidents else "全部服务正常",
            "checked_at": datetime.now(timezone.utc).isoformat(),
            "active_incidents": [self.event_public(item, include_private) for item in active_incidents],
            "recent_events": [self.event_public(item, include_private) for item in recent_notices],
            "affected_nodes": affected_nodes,
            "active_incident_count": len(active_incidents),
            "node_count": len(self.nodes),
            "is_admin": bool(include_private),
        }

    def webhook_content(self, event, action):
        title = event.get("title") or "CLIProxyAPI 状态更新"
        nodes = ", ".join(event.get("affected_nodes") or []) or "-"
        lines = [
            f"**事件**: {title}",
            f"**状态**: {action}",
            f"**影响节点**: {nodes}",
            f"**原因**: {event.get('reason') or event.get('summary') or '-'}",
        ]
        if event.get("started_at"):
            lines.append(f"**开始时间**: {event.get('started_at')}")
        if event.get("resolved_at"):
            lines.append(f"**恢复时间**: {event.get('resolved_at')}")
            lines.append(f"**持续时间**: {self.format_event_duration(event.get('started_at'), event.get('resolved_at'))}")
        if event.get("summary"):
            lines.append(f"**说明**: {event.get('summary')}")
        lines.append(f"**详情**: {self.status_page_url()}")
        return "\n".join(lines)

    def alerts_muted(self):
        if not self.alert_mute_loader:
            return False
        try:
            return bool((self.alert_mute_loader() or {}).get("muted"))
        except Exception as exc:
            print(f"[StatusEvents] Alert mute state unavailable: {exc}")
            return False

    def _build_card(self, title, content, template="orange"):
        return {
            "config": {"wide_screen_mode": True},
            "header": {
                "template": template or "orange",
                "title": {"tag": "plain_text", "content": title},
            },
            "elements": [
                {
                    "tag": "div",
                    "text": {"tag": "lark_md", "content": content},
                }
            ],
        }

    def send_webhook(self, event, action, template="orange"):
        if not event:
            return False
        if self.alerts_muted():
            print(f"[StatusEvents] Feishu alert muted: {event.get('title') or 'CLIProxyAPI 状态更新'} / {action}")
            return False
        chat_id = getattr(self.config, "FEISHU_APPROVAL_CHAT_ID", "")
        title = f"{event.get('title') or 'CLIProxyAPI 状态更新'}"
        card = self._build_card(title, self.webhook_content(event, action), template)
        sent = self.feishu.send_feishu_card_to_chat(chat_id, card)
        if sent:
            self.portal_state.mark_status_event_notified(event.get("id"))
        return sent

    def node_health_alert_delay_seconds(self):
        try:
            return max(0, int(getattr(self.config, "STATUS_NODE_HEALTH_ALERT_DELAY_SECONDS", 300) or 0))
        except (TypeError, ValueError):
            return 300

    def node_health_alert_due(self, event):
        if not event or event.get("notified_at"):
            return False
        try:
            started = datetime.fromisoformat(str(event.get("started_at") or "").replace("Z", "+00:00"))
        except Exception:
            return False
        elapsed = (datetime.now(timezone.utc) - started.astimezone(timezone.utc)).total_seconds()
        return elapsed >= self.node_health_alert_delay_seconds()

    def normalize_monitor_result(self, result):
        result = dict(result or {})
        payload = result.get("payload") if isinstance(result.get("payload"), dict) else {}
        checks = payload.get("checks") if isinstance(payload.get("checks"), dict) else {}
        return {
            "url": result.get("url") or "",
            "status_code": result.get("status_code") or 0,
            "reason": result.get("reason") or "",
            "payload_status": payload.get("status") or "",
            "checks": checks,
        }

    def record_nlb_monitor_event(self, event):
        node = str((event or {}).get("node") or "").strip()
        status = str((event or {}).get("status") or "").strip().lower()
        if not node or status not in {"healthy", "unhealthy"}:
            return None
        result = self.normalize_monitor_result((event or {}).get("result"))
        reason = str((event or {}).get("reason") or result.get("reason") or "").strip()
        dedupe_key = f"node_health:{node}"

        if status == "unhealthy":
            saved = self.portal_state.upsert_status_event(
                event_type="node_health",
                dedupe_key=dedupe_key,
                status="open",
                severity="warning",
                title=f"{node} 节点异常",
                summary=f"{node} 探活失败，NLB 可能已将该节点从服务池摘除。",
                reason=reason or "health check failed",
                affected_nodes=[node],
                metadata={"monitor_result": result},
            )
            if self.node_health_alert_due(saved.get("event")):
                self.send_webhook(saved.get("event"), "故障开始", template="red")
            return saved

        existing_events = self.portal_state.list_status_events(500)
        existing_event = next((item for item in existing_events if item.get("dedupe_key") == dedupe_key), {})
        was_notified = bool(existing_event.get("notified_at"))
        resolved = self.portal_state.resolve_status_event(
            dedupe_key,
            summary=f"{node} 探活恢复，节点已重新处于健康状态。",
            reason=reason or "ok",
            metadata={"recovery_result": result, "initial": bool((event or {}).get("initial"))},
        )
        if resolved.get("changed") and was_notified:
            self.send_webhook(resolved.get("event"), "故障恢复", template="green")
        return resolved

    def daily_usage_record_snapshot(self):
        today = self.beijing_today()
        lookback_days = max(30, self.int_usage_value(getattr(self.config, "STATUS_USAGE_RECORD_LOOKBACK_DAYS", 365)) or 365)
        today_sql = self.sql_literal(today)
        sql = f"""
WITH daily AS (
    SELECT
        ((s."endTime" + interval '8 hours')::date)::text AS date,
        count(*)::bigint AS total_requests,
        coalesce(sum(s.total_tokens), 0)::bigint AS total_tokens
    FROM "LiteLLM_SpendLogs" s
    WHERE s."endTime" >= ({today_sql}::date::timestamp - interval '8 hours' - interval '{lookback_days} days')
    GROUP BY 1
), record_row AS (
    SELECT date, total_tokens, total_requests
    FROM daily
    WHERE date <> {today_sql}
    ORDER BY total_tokens DESC, date DESC
    LIMIT 1
), today_row AS (
    SELECT date, total_tokens, total_requests
    FROM daily
    WHERE date = {today_sql}
)
SELECT json_build_object(
    'today', {today_sql},
    'today_tokens', coalesce((SELECT total_tokens FROM today_row), 0),
    'today_requests', coalesce((SELECT total_requests FROM today_row), 0),
    'previous_record_date', (SELECT date FROM record_row),
    'previous_record_tokens', coalesce((SELECT total_tokens FROM record_row), 0),
    'previous_record_requests', coalesce((SELECT total_requests FROM record_row), 0),
    'source', 'litellm_spendlogs'
);
"""
        payload = self.litellm_psql_json(sql, timeout=15)
        if not payload:
            rows = self.database.get_daily_usage_history()
            record = None
            today_row = None
            for row in rows:
                if row.get("date") == today:
                    today_row = row
                elif record is None or self.int_usage_value(row.get("total_tokens")) > self.int_usage_value(record.get("total_tokens")):
                    record = row
            payload = {
                "today": today,
                "today_tokens": self.int_usage_value((today_row or {}).get("total_tokens")),
                "today_requests": self.int_usage_value((today_row or {}).get("total_requests")),
                "previous_record_date": (record or {}).get("date"),
                "previous_record_tokens": self.int_usage_value((record or {}).get("total_tokens")),
                "previous_record_requests": self.int_usage_value((record or {}).get("total_requests")),
                "source": "key_portal_sqlite_fallback",
            }

        summary, err = self.usage_summary_loader()
        if not err and summary and summary.get("today") == today:
            payload["today_tokens"] = max(self.int_usage_value(payload.get("today_tokens")), self.int_usage_value(summary.get("today_tokens")))
            payload["today_requests"] = max(self.int_usage_value(payload.get("today_requests")), self.int_usage_value(summary.get("today_requests")))
        return payload

    def daily_model_group_spend_snapshot(self, groups):
        today = self.beijing_today()
        group_values = [str(group or "").strip().lower() for group in (groups or []) if str(group or "").strip()]
        if not group_values:
            return {"today": today, "groups": [], "source": "litellm_spendlogs"}
        group_sql = ", ".join(self.sql_literal(group) for group in group_values)
        today_sql = self.sql_literal(today)
        sql = f"""
WITH rows AS (
    SELECT
        CASE
            WHEN lower(coalesce(v.metadata->>'model_group', '')) IN ('common', 'claude', 'deepseek', 'gemini')
                THEN lower(coalesce(v.metadata->>'model_group', ''))
            ELSE 'common'
        END AS model_group,
        coalesce(nullif(v.metadata->>'email', ''), nullif(v.user_id, ''), nullif(s."user", ''), 'unknown') AS user_email,
        coalesce(s.total_tokens, 0)::bigint AS total_tokens,
        coalesce(s.spend, 0)::float8 AS spend_usd
    FROM "LiteLLM_SpendLogs" s
    LEFT JOIN "LiteLLM_VerificationToken" v ON s.api_key = v.token
    WHERE s."endTime" >= ({today_sql}::date::timestamp - interval '8 hours')
      AND s."endTime" < ({today_sql}::date::timestamp + interval '16 hours')
), grouped AS (
    SELECT
        model_group,
        count(*)::bigint AS requests,
        coalesce(sum(total_tokens), 0)::bigint AS tokens,
        coalesce(sum(spend_usd), 0)::float8 AS spend_usd,
        count(distinct user_email)::bigint AS users
    FROM rows
    WHERE model_group IN ({group_sql})
    GROUP BY 1
), top_users AS (
    SELECT
        model_group,
        json_agg(json_build_object('email', user_email, 'spend_usd', spend_usd, 'tokens', tokens) ORDER BY spend_usd DESC) AS top_users
    FROM (
        SELECT
            model_group,
            user_email,
            coalesce(sum(spend_usd), 0)::float8 AS spend_usd,
            coalesce(sum(total_tokens), 0)::bigint AS tokens,
            row_number() OVER (PARTITION BY model_group ORDER BY coalesce(sum(spend_usd), 0)::float8 DESC) AS rn
        FROM rows
        WHERE model_group IN ({group_sql})
        GROUP BY 1, 2
    ) ranked
    WHERE rn <= 5
    GROUP BY 1
)
SELECT json_build_object(
    'today', {today_sql},
    'source', 'litellm_spendlogs',
    'groups', coalesce(json_agg(json_build_object(
        'model_group', grouped.model_group,
        'requests', grouped.requests,
        'tokens', grouped.tokens,
        'spend_usd', grouped.spend_usd,
        'users', grouped.users,
        'top_users', coalesce(top_users.top_users, '[]'::json)
    ) ORDER BY grouped.spend_usd DESC), '[]'::json)
)
FROM grouped
LEFT JOIN top_users ON top_users.model_group = grouped.model_group;
"""
        payload = self.litellm_psql_json(sql, timeout=15)
        if payload is None:
            return {"today": today, "groups": [], "source": "litellm_spendlogs", "error": "litellm spend query failed"}
        return payload

    def check_model_group_spend_alerts(self):
        if not getattr(self.config, "MODEL_GROUP_SPEND_ALERT_ENABLED", True):
            return []
        threshold = float(getattr(self.config, "MODEL_GROUP_SPEND_ALERT_THRESHOLD_USD", 100.0) or 100.0)
        groups = [
            group for group in getattr(self.config, "MODEL_GROUP_SPEND_ALERT_GROUPS", ["claude", "deepseek"])
            if group in {"claude", "deepseek"}
        ]
        snapshot = self.daily_model_group_spend_snapshot(groups)
        today = snapshot.get("today") or self.beijing_today()
        results = []
        labels = {"claude": "Claude", "deepseek": "DeepSeek"}
        if snapshot.get("error"):
            saved = self.portal_state.upsert_status_event(
                event_type="model_group_spend_monitor",
                dedupe_key=f"model_group_spend_monitor:{today}",
                status="open",
                severity="warning",
                title="模型组费用监控异常",
                summary="无法读取 LiteLLM spend 数据，Claude/DeepSeek 超额告警可能失效。",
                reason=snapshot.get("error"),
                affected_nodes=[],
                metadata=snapshot,
            )
            if saved.get("created") or saved.get("changed"):
                self.send_webhook(saved.get("event"), "监控异常", template="red")
            return [saved]

        self.portal_state.resolve_status_event(
            f"model_group_spend_monitor:{today}",
            summary="LiteLLM spend 数据读取恢复，模型组费用监控正常。",
            reason="ok",
            metadata=snapshot,
        )
        for row in snapshot.get("groups") or []:
            group = str(row.get("model_group") or "").lower()
            spend = float(row.get("spend_usd") or 0)
            if not group or group not in {"claude", "deepseek"} or spend < threshold:
                continue
            threshold_bucket = int(spend // threshold)
            threshold_amount = threshold_bucket * threshold
            title = f"{labels.get(group, group)} 今日用量超过阈值"
            metadata = {**snapshot, "triggered_group": row, "threshold_usd": threshold, "threshold_bucket": threshold_bucket, "threshold_amount_usd": threshold_amount}
            saved = self.portal_state.upsert_status_event(
                event_type="model_group_spend",
                dedupe_key=f"model_group_spend:{today}:{group}:{threshold:g}:{threshold_bucket}",
                status="notice",
                severity="warning",
                title=title,
                summary=f"{labels.get(group, group)} 今日费用 ${spend:.2f}，已超过阈值 ${threshold_amount:g}。",
                reason="daily spend threshold",
                affected_nodes=[],
                metadata=metadata,
            )
            if saved.get("created"):
                event = saved.get("event") or {}
                top_users = row.get("top_users") or []
                user_lines = []
                for item in top_users[:5]:
                    email = item.get("email") or "unknown"
                    user_lines.append(f"- {email}: ${float(item.get('spend_usd') or 0):.2f}, {self.format_compact_number(item.get('tokens'))} tokens")
                content = "\n".join([
                    f"**事件**: {title}",
                    "**状态**: 阈值触发",
                    f"**模型组**: {labels.get(group, group)}",
                    f"**今日费用**: ${spend:.2f}",
                    f"**阈值**: ${threshold_amount:g}",
                    f"**请求数**: {self.format_compact_number(row.get('requests'))}",
                    f"**Tokens**: {self.format_compact_number(row.get('tokens'))}",
                    f"**用户数**: {self.format_compact_number(row.get('users'))}",
                    "**Top Users**:",
                    "\n".join(user_lines) if user_lines else "- 无",
                    f"**详情**: {self.status_page_url()}",
                ])
                if not self.alerts_muted():
                    chat_id = getattr(self.config, "FEISHU_APPROVAL_CHAT_ID", "")
                    card = self._build_card(title, content, "orange")
                    sent = self.feishu.send_feishu_card_to_chat(chat_id, card)
                    if sent:
                        self.portal_state.mark_status_event_notified(event.get("id"))
                else:
                    print(f"[StatusEvents] Feishu alert muted: {title}")
            results.append(saved)
        return results

    def check_daily_usage_record(self):
        if not getattr(self.config, "STATUS_USAGE_RECORD_ENABLED", True):
            return None
        snapshot = self.daily_usage_record_snapshot()
        today_tokens = self.int_usage_value(snapshot.get("today_tokens"))
        record_tokens = self.int_usage_value(snapshot.get("previous_record_tokens"))
        if record_tokens <= 0 or today_tokens <= record_tokens:
            return None

        today = snapshot.get("today") or self.beijing_today()
        previous_date = snapshot.get("previous_record_date") or "-"
        diff = today_tokens - record_tokens
        saved = self.portal_state.upsert_status_event(
            event_type="usage_record",
            dedupe_key=f"usage_record:{today}:tokens",
            status="notice",
            severity="info",
            title="今日用量突破历史新高",
            summary=f"今日 Tokens {self.format_compact_number(today_tokens)}，超过 {previous_date} 的历史最高 {self.format_compact_number(record_tokens)}。",
            reason="daily token record",
            affected_nodes=[],
            metadata={**snapshot, "delta_tokens": diff},
        )
        if saved.get("created"):
            event = saved.get("event") or {}
            content = "\n".join([
                "**事件**: 今日用量突破历史新高",
                f"**今日 Tokens**: {self.format_compact_number(today_tokens)}",
                f"**历史最高**: {self.format_compact_number(record_tokens)} ({previous_date})",
                f"**突破幅度**: +{self.format_compact_number(diff)}",
                f"**入口**: {self.portal_home_url()}",
            ])
            if not self.alerts_muted():
                chat_id = getattr(self.config, "FEISHU_APPROVAL_CHAT_ID", "")
                card = self._build_card("今日用量突破历史新高", content, "blue")
                sent = self.feishu.send_feishu_card_to_chat(chat_id, card)
                if sent:
                    self.portal_state.mark_status_event_notified(event.get("id"))
            else:
                print("[StatusEvents] Feishu alert muted: 今日用量突破历史新高")
        return saved
