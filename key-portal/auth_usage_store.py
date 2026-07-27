"""Durable minute aggregates for CLIProxyAPI auth-file usage."""

import re
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import psycopg
    from psycopg.rows import dict_row
except Exception:  # pragma: no cover - production dependency
    psycopg = None
    dict_row = None

UTC = timezone.utc
WINDOW_SECONDS = {
    "last_1h": 3600,
    "last_5h": 5 * 3600,
    "last_24h": 24 * 3600,
    "last_7d": 7 * 24 * 3600,
}


def psycopg_database_url(database_url):
    text = str(database_url or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except Exception:
        return text
    if not parts.query:
        return text
    query = urlencode([
        (key, value)
        for key, value in parse_qsl(parts.query, keep_blank_values=True)
        if key.lower() != "pgbouncer"
    ])
    return urlunsplit((parts.scheme, parts.netloc, parts.path, query, parts.fragment))


class AuthUsageStore:
    def __init__(self, database_url="", retention_days=31):
        self.database_url = psycopg_database_url(database_url)
        self.retention_days = max(8, int(retention_days or 31))
        self.enabled = bool(self.database_url and psycopg)
        self._schema_ready = False
        self._schema_lock = threading.Lock()
        self._ingest_lock = threading.Lock()
        self._last_cleanup = 0.0
        self._last_errors = []

    def connect(self):
        if not self.enabled:
            return None
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def ensure_schema(self):
        if not self.enabled:
            return False
        with self._schema_lock:
            if self._schema_ready:
                return True
            with self.connect() as conn:
                with conn.cursor() as cursor:
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_portal_auth_usage_minute (
                            bucket_start timestamptz NOT NULL,
                            node text NOT NULL,
                            auth_index text NOT NULL DEFAULT '',
                            source text NOT NULL DEFAULT '',
                            requests bigint NOT NULL DEFAULT 0,
                            success bigint NOT NULL DEFAULT 0,
                            failure bigint NOT NULL DEFAULT 0,
                            tokens bigint NOT NULL DEFAULT 0,
                            input_tokens bigint NOT NULL DEFAULT 0,
                            output_tokens bigint NOT NULL DEFAULT 0,
                            cached_tokens bigint NOT NULL DEFAULT 0,
                            reasoning_tokens bigint NOT NULL DEFAULT 0,
                            last_request_at timestamptz,
                            last_error_at timestamptz,
                            last_error_status text NOT NULL DEFAULT '',
                            last_error_message text NOT NULL DEFAULT '',
                            updated_at timestamptz NOT NULL DEFAULT now(),
                            PRIMARY KEY (bucket_start, node, auth_index, source)
                        )
                        """
                    )
                    cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_key_portal_auth_usage_lookup "
                        "ON key_portal_auth_usage_minute(node, auth_index, bucket_start DESC)"
                    )
                    cursor.execute(
                        "CREATE INDEX IF NOT EXISTS idx_key_portal_auth_usage_time "
                        "ON key_portal_auth_usage_minute(bucket_start DESC)"
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_portal_auth_usage_state (
                            id smallint PRIMARY KEY CHECK (id = 1),
                            continuous_since timestamptz NOT NULL,
                            last_collected_at timestamptz NOT NULL
                        )
                        """
                    )
                    cursor.execute(
                        """
                        INSERT INTO key_portal_auth_usage_state (id, continuous_since, last_collected_at)
                        VALUES (1, now(), now())
                        ON CONFLICT (id) DO NOTHING
                        """
                    )
                    cursor.execute(
                        """
                        UPDATE key_portal_auth_usage_state
                        SET continuous_since = now(), last_collected_at = now()
                        WHERE id = 1 AND last_collected_at < now() - interval '60 seconds'
                        """
                    )
                    cursor.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_portal_auth_usage_node_state (
                            node text PRIMARY KEY,
                            continuous_since timestamptz NOT NULL,
                            last_collected_at timestamptz NOT NULL
                        )
                        """
                    )
                conn.commit()
            self._schema_ready = True
            return True

    def reset_collection_coverage(self, node_names):
        if not self.ensure_schema():
            return False
        nodes = sorted({str(node or "").strip() for node in node_names or [] if str(node or "").strip()})
        with self.connect() as conn:
            with conn.cursor() as cursor:
                for node in nodes:
                    cursor.execute(
                        """
                        INSERT INTO key_portal_auth_usage_node_state (node, continuous_since, last_collected_at)
                        VALUES (%s, now(), now())
                        ON CONFLICT (node) DO UPDATE SET
                            continuous_since = now(), last_collected_at = now()
                        """,
                        (node,),
                    )
            conn.commit()
        return True

    @staticmethod
    def _timestamp(value):
        text = str(value or "").strip()
        if not text:
            return None
        text = re.sub(
            r"(\.\d{6})\d+(?=(?:Z|[+-]\d{2}:\d{2})$)",
            r"\1",
            text,
            flags=re.IGNORECASE,
        )
        if text.endswith(("Z", "z")):
            text = text[:-1] + "+00:00"
        try:
            parsed = datetime.fromisoformat(text)
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)

    @staticmethod
    def _failed(value):
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes"}
        return bool(value)

    @classmethod
    def aggregate_records(cls, node, records):
        grouped = {}
        for record in records or []:
            if not isinstance(record, dict):
                continue
            occurred_at = cls._timestamp(record.get("timestamp"))
            if not occurred_at:
                continue
            bucket_start = occurred_at.replace(second=0, microsecond=0)
            auth_index = str(record.get("auth_index") or record.get("authIndex") or "").strip()
            source = str(record.get("source") or record.get("provider") or "").strip()
            key = (bucket_start, str(node or "unknown"), auth_index, source)
            row = grouped.setdefault(key, {
                "bucket_start": bucket_start,
                "node": str(node or "unknown"),
                "auth_index": auth_index,
                "source": source,
                "requests": 0,
                "success": 0,
                "failure": 0,
                "tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "last_request_at": occurred_at,
                "last_error_at": None,
                "last_error_status": "",
                "last_error_message": "",
            })
            token_data = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
            input_tokens = int(token_data.get("input_tokens", 0) or 0)
            output_tokens = int(token_data.get("output_tokens", 0) or 0)
            cached_tokens = int(token_data.get("cached_tokens", 0) or 0)
            reasoning_tokens = int(token_data.get("reasoning_tokens", 0) or 0)
            total_tokens = int(token_data.get("total_tokens", 0) or 0)
            if not total_tokens:
                total_tokens = input_tokens + output_tokens + reasoning_tokens
            if not total_tokens:
                total_tokens += cached_tokens
            failed = cls._failed(record.get("failed"))
            row["requests"] += 1
            row["failure" if failed else "success"] += 1
            row["tokens"] += total_tokens
            row["input_tokens"] += input_tokens
            row["output_tokens"] += output_tokens
            row["cached_tokens"] += cached_tokens
            row["reasoning_tokens"] += reasoning_tokens
            row["last_request_at"] = max(row["last_request_at"], occurred_at)
            if failed and (row["last_error_at"] is None or occurred_at >= row["last_error_at"]):
                failure = record.get("fail") if isinstance(record.get("fail"), dict) else {}
                row["last_error_at"] = occurred_at
                row["last_error_status"] = str(failure.get("status_code") or failure.get("status") or "")
                row["last_error_message"] = str(failure.get("body") or failure.get("message") or "请求失败")[:1000]
        return list(grouped.values())

    def ingest_results(self, results, collection_complete=True, coverage_nodes=None):
        if not self.ensure_schema():
            return {"records": 0, "buckets": 0, "errors": [{"node": "cluster", "error": "auth usage store unavailable"}]}
        rows = []
        errors = []
        record_count = 0
        for node, payload, error in results or []:
            if error:
                errors.append({"node": node, "error": str(error)})
                continue
            records = payload if isinstance(payload, list) else []
            record_count += len(records)
            rows.extend(self.aggregate_records(node, records))

        with self._ingest_lock:
            with self.connect() as conn:
                with conn.cursor() as cursor:
                    if rows:
                        cursor.executemany(
                            """
                            INSERT INTO key_portal_auth_usage_minute (
                                bucket_start, node, auth_index, source,
                                requests, success, failure, tokens,
                                input_tokens, output_tokens, cached_tokens, reasoning_tokens,
                                last_request_at, last_error_at, last_error_status, last_error_message
                            ) VALUES (
                                %(bucket_start)s, %(node)s, %(auth_index)s, %(source)s,
                                %(requests)s, %(success)s, %(failure)s, %(tokens)s,
                                %(input_tokens)s, %(output_tokens)s, %(cached_tokens)s, %(reasoning_tokens)s,
                                %(last_request_at)s, %(last_error_at)s, %(last_error_status)s, %(last_error_message)s
                            )
                            ON CONFLICT (bucket_start, node, auth_index, source) DO UPDATE SET
                                requests = key_portal_auth_usage_minute.requests + EXCLUDED.requests,
                                success = key_portal_auth_usage_minute.success + EXCLUDED.success,
                                failure = key_portal_auth_usage_minute.failure + EXCLUDED.failure,
                                tokens = key_portal_auth_usage_minute.tokens + EXCLUDED.tokens,
                                input_tokens = key_portal_auth_usage_minute.input_tokens + EXCLUDED.input_tokens,
                                output_tokens = key_portal_auth_usage_minute.output_tokens + EXCLUDED.output_tokens,
                                cached_tokens = key_portal_auth_usage_minute.cached_tokens + EXCLUDED.cached_tokens,
                                reasoning_tokens = key_portal_auth_usage_minute.reasoning_tokens + EXCLUDED.reasoning_tokens,
                                last_request_at = greatest(key_portal_auth_usage_minute.last_request_at, EXCLUDED.last_request_at),
                                last_error_at = greatest(key_portal_auth_usage_minute.last_error_at, EXCLUDED.last_error_at),
                                last_error_status = CASE
                                    WHEN key_portal_auth_usage_minute.last_error_at IS NULL
                                      OR EXCLUDED.last_error_at >= key_portal_auth_usage_minute.last_error_at
                                    THEN EXCLUDED.last_error_status ELSE key_portal_auth_usage_minute.last_error_status END,
                                last_error_message = CASE
                                    WHEN key_portal_auth_usage_minute.last_error_at IS NULL
                                      OR EXCLUDED.last_error_at >= key_portal_auth_usage_minute.last_error_at
                                    THEN EXCLUDED.last_error_message ELSE key_portal_auth_usage_minute.last_error_message END,
                                updated_at = now()
                            """,
                            rows,
                        )
                    tracked_nodes = coverage_nodes
                    if tracked_nodes is None:
                        tracked_nodes = [node for node, _, _ in results or []]
                    tracked_nodes = sorted({
                        str(node or "").strip()
                        for node in tracked_nodes or []
                        if str(node or "").strip()
                    })
                    for node in tracked_nodes:
                        if collection_complete:
                            cursor.execute(
                                """
                                INSERT INTO key_portal_auth_usage_node_state (node, continuous_since, last_collected_at)
                                VALUES (%s, now(), now())
                                ON CONFLICT (node) DO UPDATE SET
                                    continuous_since = CASE
                                        WHEN key_portal_auth_usage_node_state.last_collected_at < now() - interval '10 seconds'
                                        THEN now()
                                        ELSE key_portal_auth_usage_node_state.continuous_since
                                    END,
                                    last_collected_at = now()
                                """,
                                (node,),
                            )
                        else:
                            cursor.execute(
                                """
                                INSERT INTO key_portal_auth_usage_node_state (node, continuous_since, last_collected_at)
                                VALUES (%s, now(), now())
                                ON CONFLICT (node) DO UPDATE SET
                                    continuous_since = now(), last_collected_at = now()
                                """,
                                (node,),
                            )
                    if time.monotonic() - self._last_cleanup >= 3600:
                        cursor.execute(
                            "DELETE FROM key_portal_auth_usage_minute "
                            f"WHERE bucket_start < now() - interval '{self.retention_days} days'"
                        )
                        self._last_cleanup = time.monotonic()
                conn.commit()
        self._last_errors = errors
        return {"records": record_count, "buckets": len(rows), "errors": errors}

    @staticmethod
    def _window_sql(cutoff):
        metrics = {
            "requests": "requests",
            "success": "success",
            "failure": "failure",
            "tokens": "tokens",
            "input_tokens": "input_tokens",
            "output_tokens": "output_tokens",
            "cached_tokens": "cached_tokens",
            "reasoning_tokens": "reasoning_tokens",
        }
        fields = [
            f"'{name}', coalesce(sum({column}) FILTER (WHERE bucket_start >= {cutoff}), 0)::bigint"
            for name, column in metrics.items()
        ]
        fields.extend([
            f"'observed_start_at', min(bucket_start) FILTER (WHERE bucket_start >= {cutoff})",
            f"'observed_end_at', max(last_request_at) FILTER (WHERE bucket_start >= {cutoff})",
        ])
        return "json_build_object(" + ", ".join(fields) + ")"

    @staticmethod
    def _empty_history(now):
        hour_start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)
        day_start_7 = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)
        day_start_30 = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)

        def buckets(start, count, step):
            return [{"time": (start + step * index).isoformat().replace("+00:00", "Z"), "requests": 0, "success": 0, "failure": 0} for index in range(count)]

        return {
            "24h": buckets(hour_start, 24, timedelta(hours=1)),
            "7d": buckets(day_start_7, 7, timedelta(days=1)),
            "30d": buckets(day_start_30, 30, timedelta(days=1)),
        }

    def load_snapshot(self):
        if not self.ensure_schema():
            return {"auth_files": [], "errors": [{"node": "cluster", "error": "auth usage store unavailable"}], "coverage_seconds": 0}
        windows = {
            "today": "bounds.today_start",
            "last_1h": "bounds.last_1h",
            "last_5h": "bounds.last_5h",
            "last_24h": "bounds.last_24h",
            "last_7d": "bounds.last_7d",
            "total": "bounds.last_30d",
        }
        select_windows = ",\n                ".join(
            f"{self._window_sql(cutoff)} AS {name}"
            for name, cutoff in windows.items()
        )
        sql = f"""
WITH bounds AS (
    SELECT
        now() AS now_utc,
        (date_trunc('day', now() AT TIME ZONE 'Asia/Shanghai') AT TIME ZONE 'Asia/Shanghai') AS today_start,
        now() - interval '1 hour' AS last_1h,
        now() - interval '5 hours' AS last_5h,
        now() - interval '24 hours' AS last_24h,
        now() - interval '7 days' AS last_7d,
        now() - interval '30 days' AS last_30d
), grouped AS (
    SELECT
        node,
        auth_index,
        source,
        {select_windows},
        max(last_request_at) AS last_request_at,
        (array_agg(last_error_at ORDER BY last_error_at DESC NULLS LAST) FILTER (WHERE last_error_at IS NOT NULL))[1] AS last_error_at,
        (array_agg(last_error_status ORDER BY last_error_at DESC NULLS LAST) FILTER (WHERE last_error_at IS NOT NULL))[1] AS last_error_status,
        (array_agg(last_error_message ORDER BY last_error_at DESC NULLS LAST) FILTER (WHERE last_error_at IS NOT NULL))[1] AS last_error_message
    FROM key_portal_auth_usage_minute, bounds
    WHERE bucket_start >= bounds.last_30d
    GROUP BY node, auth_index, source
)
SELECT * FROM grouped ORDER BY node, source, auth_index;
"""
        history_sql = """
SELECT
    node,
    auth_index,
    source,
    date_trunc('hour', bucket_start) AS bucket_hour,
    sum(requests)::bigint AS requests,
    sum(success)::bigint AS success,
    sum(failure)::bigint AS failure
FROM key_portal_auth_usage_minute
WHERE bucket_start >= now() - interval '30 days'
GROUP BY node, auth_index, source, date_trunc('hour', bucket_start)
ORDER BY bucket_hour;
"""
        with self.connect() as conn:
            with conn.cursor() as cursor:
                cursor.execute("SET LOCAL statement_timeout = 30000")
                cursor.execute(sql)
                rows = [dict(row) for row in cursor.fetchall()]
                cursor.execute(history_sql)
                history_rows = [dict(row) for row in cursor.fetchall()]
                cursor.execute(
                    "SELECT node, continuous_since, last_collected_at, now() AS now_utc "
                    "FROM key_portal_auth_usage_node_state ORDER BY node"
                )
                state_rows = [dict(row) for row in cursor.fetchall()]

        now_utc = (state_rows[0].get("now_utc") if state_rows else None) or datetime.now(UTC)
        coverage_by_node = {}
        continuous_by_node = {}
        for state in state_rows:
            node = state.get("node") or ""
            continuous_since = state.get("continuous_since") or now_utc
            last_collected_at = state.get("last_collected_at") or continuous_since
            is_current = (now_utc - last_collected_at).total_seconds() <= 10
            coverage_by_node[node] = max(0, int((now_utc - continuous_since).total_seconds())) if is_current else 0
            continuous_by_node[node] = continuous_since
        coverage_seconds = min(coverage_by_node.values()) if coverage_by_node else 0
        by_key = {}
        for row in rows:
            key = (row.get("node", ""), row.get("auth_index", ""), row.get("source", ""))
            row["history"] = self._empty_history(now_utc)
            row_coverage_seconds = coverage_by_node.get(row.get("node", ""), 0)
            for name, seconds in WINDOW_SECONDS.items():
                bucket = row.get(name) if isinstance(row.get(name), dict) else {}
                bucket["complete"] = row_coverage_seconds >= seconds
                bucket["coverage_seconds"] = min(row_coverage_seconds, seconds)
                row[name] = bucket
            by_key[key] = row

        for item in history_rows:
            key = (item.get("node", ""), item.get("auth_index", ""), item.get("source", ""))
            row = by_key.get(key)
            if not row:
                continue
            bucket_hour = item.get("bucket_hour")
            if not bucket_hour:
                continue
            if bucket_hour.tzinfo is None:
                bucket_hour = bucket_hour.replace(tzinfo=UTC)
            values = {name: int(item.get(name, 0) or 0) for name in ("requests", "success", "failure")}
            for range_name, buckets in row["history"].items():
                target = bucket_hour.replace(minute=0, second=0, microsecond=0)
                if range_name != "24h":
                    target = target.replace(hour=0)
                target_text = target.isoformat().replace("+00:00", "Z")
                matched = next((bucket for bucket in buckets if bucket["time"] == target_text), None)
                if matched:
                    for name, value in values.items():
                        matched[name] += value

        return {
            "auth_files": rows,
            "errors": list(self._last_errors),
            "coverage_seconds": coverage_seconds,
            "coverage_by_node": coverage_by_node,
            "continuous_since": max(continuous_by_node.values()).isoformat().replace("+00:00", "Z") if continuous_by_node else "",
            "generated_at": now_utc.isoformat().replace("+00:00", "Z"),
        }
