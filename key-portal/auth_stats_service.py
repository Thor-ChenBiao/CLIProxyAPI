"""Auth-file usage and quota reporting for Key Portal."""

import json
import math
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta


class AuthStatsService:
    def __init__(
        self,
        portal_state,
        nodes,
        call_management_api_node,
        get_cluster_usage,
        get_cluster_auth_files,
        usage_summary_loader,
        parse_detail_time,
        parse_detail_time_utc,
        build_token_breakdown,
        usage_snapshot_loader=None,
    ):
        self.portal_state = portal_state
        self.nodes = nodes
        self.call_management_api_node = call_management_api_node
        self.get_cluster_usage = get_cluster_usage
        self.get_cluster_auth_files = get_cluster_auth_files
        self.usage_summary_loader = usage_summary_loader
        self.parse_detail_time = parse_detail_time
        self.parse_detail_time_utc = parse_detail_time_utc
        self.build_token_breakdown = build_token_breakdown
        self.usage_snapshot_loader = usage_snapshot_loader
        self.stats_cache = {"data": None, "last_update": 0, "ttl": 15, "refreshing": False}
        self.stats_cache_lock = threading.Lock()
        self.quota_fetch_cache = {"data": {}, "ttl": 1800}
        self.quota_fetch_cache_lock = threading.Lock()

    def node_management_path(self, node_name, index):
        name = str(node_name or "").strip().lower()
        if name in ("old", "node-a", "node_a", "a"):
            slug = "a"
        else:
            match = re.match(r"^node[-_]?([a-z0-9]+)$", name)
            if match:
                slug = match.group(1)
            elif 0 <= index < 26:
                slug = chr(ord("a") + index)
            else:
                slug = re.sub(r"[^a-z0-9_-]+", "-", name).strip("-") or str(index + 1)
        return f"/{slug}/management.html#/"

    def configured_nodes(self):
        return [
            {
                "name": node.get("name", ""),
                "management_path": self.node_management_path(node.get("name", ""), index),
                "management_url": node.get("url", ""),
            }
            for index, node in enumerate(self.nodes)
        ]

    @staticmethod
    def _number_or_none(value):
        if isinstance(value, bool) or value is None:
            return None
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.endswith("%"):
                text = text[:-1].strip()
            try:
                return float(text)
            except ValueError:
                return None
        return None

    @staticmethod
    def _camel_or_snake(data, snake, camel):
        if not isinstance(data, dict):
            return None
        return data.get(snake) if snake in data else data.get(camel)

    def _reset_at_iso(self, window):
        if not isinstance(window, dict):
            return ""
        reset_at = self._number_or_none(
            self._camel_or_snake(window, "reset_at", "resetAt")
            or self._camel_or_snake(window, "reset_time", "resetTime")
        )
        if reset_at and reset_at > 0:
            try:
                return datetime.utcfromtimestamp(reset_at).isoformat() + "Z"
            except (OverflowError, OSError, ValueError):
                pass
        reset_after = self._number_or_none(
            self._camel_or_snake(window, "reset_after_seconds", "resetAfterSeconds")
            or self._camel_or_snake(window, "reset_in", "resetIn")
        )
        if reset_after and reset_after > 0:
            return (datetime.utcnow() + timedelta(seconds=reset_after)).isoformat() + "Z"
        raw_reset = self._camel_or_snake(window, "resets_at", "resetsAt")
        if isinstance(raw_reset, str) and raw_reset.strip():
            return raw_reset.strip()
        return ""

    def _quota_window_from_used_percent(self, window, limit_label):
        if not isinstance(window, dict):
            return None
        used_raw = self._camel_or_snake(window, "used_percent", "usedPercent")
        if used_raw is None:
            used_raw = window.get("utilization")
        used = self._number_or_none(used_raw)
        if used is None and self._reset_at_iso(window):
            used = 100
        if used is None:
            return None
        remaining = max(0, min(100, round(100 - used, 2)))
        window_seconds = self._number_or_none(self._camel_or_snake(window, "limit_window_seconds", "limitWindowSeconds"))
        if not window_seconds:
            label = str(limit_label or "").lower()
            if "7" in label or "week" in label:
                window_seconds = 7 * 24 * 3600
            elif "5" in label:
                window_seconds = 5 * 3600
        return {
            "limit_label": limit_label,
            "used_percent": max(0, min(100, round(used, 2))),
            "remaining_percent": remaining,
            "reset_at": self._reset_at_iso(window),
            "limit_window_seconds": window_seconds,
        }

    def _pick_claude_seven_day_window(self, payload):
        candidates = [
            ("seven_day", "7d 原生窗口"),
            ("seven_day_oauth_apps", "7d OAuth Apps"),
            ("seven_day_opus", "7d Opus"),
            ("seven_day_sonnet", "7d Sonnet"),
            ("seven_day_cowork", "7d Cowork"),
        ]
        parsed = []
        for key, label in candidates:
            item = self._quota_window_from_used_percent(payload.get(key), label)
            if item:
                parsed.append(item)
        if not parsed:
            return None
        return sorted(parsed, key=lambda item: item.get("remaining_percent", 101))[0]

    def _quota_cache_key(self, auth_file):
        return "|".join([
            str(auth_file.get("node") or ""),
            str(auth_file.get("auth_index") or auth_file.get("authIndex") or ""),
            str(auth_file.get("provider") or auth_file.get("type") or "").lower(),
        ])

    def _auth_chatgpt_account_id(self, auth_file):
        id_token = auth_file.get("id_token")
        if isinstance(id_token, dict):
            account_id = id_token.get("chatgpt_account_id") or id_token.get("chatgptAccountId")
            if isinstance(account_id, str) and account_id.strip():
                return account_id.strip()
        return ""

    def _quota_api_payload(self, auth_file):
        provider = str(auth_file.get("provider") or auth_file.get("type") or "").strip().lower()
        auth_index = str(auth_file.get("auth_index") or auth_file.get("authIndex") or "").strip()
        if not auth_index:
            return None, "auth_index missing"
        if provider == "codex":
            headers = {
                "Authorization": "Bearer $TOKEN$",
                "Content-Type": "application/json",
                "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
            }
            account_id = self._auth_chatgpt_account_id(auth_file)
            if account_id:
                headers["Chatgpt-Account-Id"] = account_id
            return {
                "auth_index": auth_index,
                "authIndex": auth_index,
                "method": "GET",
                "url": "https://chatgpt.com/backend-api/wham/usage",
                "header": headers,
            }, ""
        if provider == "claude":
            return {
                "auth_index": auth_index,
                "authIndex": auth_index,
                "method": "GET",
                "url": "https://api.anthropic.com/api/oauth/usage",
                "header": {
                    "Authorization": "Bearer $TOKEN$",
                    "Content-Type": "application/json",
                    "anthropic-beta": "oauth-2025-04-20",
                },
            }, ""
        return None, "unsupported provider"

    @staticmethod
    def _api_call_body_json(data):
        if not isinstance(data, dict):
            return None
        body = data.get("body")
        if isinstance(body, dict):
            return body
        if isinstance(body, str):
            text = body.strip()
            if not text:
                return None
            try:
                return json.loads(text)
            except ValueError:
                return None
        return None

    def _quota_cache_get(self, cache_key):
        now = time.time()
        with self.quota_fetch_cache_lock:
            entry = self.quota_fetch_cache["data"].get(cache_key)
            if not entry:
                return None
            if now - entry.get("last_update", 0) > self.quota_fetch_cache["ttl"]:
                return None
            return dict(entry.get("snapshot") or {})

    def _quota_cache_set(self, cache_key, snapshot):
        with self.quota_fetch_cache_lock:
            self.quota_fetch_cache["data"][cache_key] = {
                "snapshot": snapshot,
                "last_update": time.time(),
            }

    def _fetch_quota_snapshot(self, auth_file):
        provider = str(auth_file.get("provider") or auth_file.get("type") or "").strip().lower()
        payload, skipped = self._quota_api_payload(auth_file)
        if not payload:
            return None, skipped
        node_by_name = {node.get("name"): node for node in self.nodes}
        node = node_by_name.get(auth_file.get("node"))
        if not node:
            return None, "node missing"
        data, err = self.call_management_api_node(node, "POST", "/v0/management/api-call", data=payload, timeout=30)
        if err:
            return None, err
        status_code = int(data.get("status_code") or data.get("statusCode") or 0) if isinstance(data, dict) else 0
        if status_code < 200 or status_code >= 300:
            return None, f"provider quota API returned {status_code or 'unknown'}"
        body = self._api_call_body_json(data)
        if not body:
            return None, "empty quota response"
        return {
            "type": provider,
            "payload": body,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "source": "proxy_api_call_cache",
        }, ""

    def attach_proxy_quota_snapshots(self, files):
        eligible = []
        errors = []
        for auth_file in files:
            if not isinstance(auth_file, dict) or isinstance(auth_file.get("quota_snapshot"), dict):
                continue
            provider = str(auth_file.get("provider") or auth_file.get("type") or "").strip().lower()
            if provider not in ("codex", "claude"):
                continue
            cache_key = self._quota_cache_key(auth_file)
            cached = self._quota_cache_get(cache_key)
            if cached:
                auth_file["quota_snapshot"] = cached
                continue
            if auth_file.get("disabled") or auth_file.get("unavailable"):
                continue
            eligible.append((cache_key, auth_file))

        def fetch(item):
            cache_key, auth_file = item
            snapshot, err = self._fetch_quota_snapshot(auth_file)
            return cache_key, auth_file, snapshot, err

        with ThreadPoolExecutor(max_workers=min(4, max(1, len(eligible)))) as executor:
            futures = [executor.submit(fetch, item) for item in eligible]
            for future in as_completed(futures):
                cache_key, auth_file, snapshot, err = future.result()
                if snapshot:
                    self._quota_cache_set(cache_key, snapshot)
                    auth_file["quota_snapshot"] = snapshot
                elif err and err != "unsupported provider":
                    errors.append({
                        "node": auth_file.get("node") or "unknown",
                        "auth_index": auth_file.get("auth_index") or auth_file.get("authIndex") or "",
                        "error": f"quota fetch skipped: {err}",
                    })
        return errors

    def quota_from_snapshot(self, auth_file):
        provider = str(auth_file.get("provider") or auth_file.get("type") or "").strip().lower()
        snapshot = auth_file.get("quota_snapshot") if isinstance(auth_file, dict) else None
        if not isinstance(snapshot, dict):
            return self.quota_not_polled(provider)
        payload = snapshot.get("payload")
        if not isinstance(payload, dict):
            return self.quota_not_polled(provider)
        quota_type = str(snapshot.get("type") or provider).strip().lower()
        if quota_type == "codex":
            rate_limit = payload.get("rate_limit") or payload.get("rateLimit") or {}
            primary = rate_limit.get("primary_window") or rate_limit.get("primaryWindow")
            secondary = rate_limit.get("secondary_window") or rate_limit.get("secondaryWindow")
            return {
                "status": "success",
                "provider": "codex",
                "plan_type": payload.get("plan_type") or payload.get("planType") or auth_file.get("plan_type") or "",
                "fetched_at": snapshot.get("fetched_at") or "",
                "source": "proxy_snapshot",
                "windows": {
                    "last_5h": self._quota_window_from_used_percent(primary, "5h 原生窗口"),
                    "last_7d": self._quota_window_from_used_percent(secondary, "7d 原生窗口"),
                },
            }
        if quota_type == "claude":
            return {
                "status": "success",
                "provider": "claude",
                "fetched_at": snapshot.get("fetched_at") or "",
                "source": "proxy_snapshot",
                "windows": {
                    "last_5h": self._quota_window_from_used_percent(payload.get("five_hour"), "5h 原生窗口"),
                    "last_7d": self._pick_claude_seven_day_window(payload),
                },
                "extra_usage": payload.get("extra_usage"),
            }
        return self.quota_not_polled(provider)

    @staticmethod
    def quota_not_polled(provider):
        return {
            "status": "not_polled",
            "provider": provider,
            "message": "Key Portal 不主动查询 provider 原生额度；如需额度快照，请以 proxy 暴露的数据为准。",
            "windows": {},
        }

    @staticmethod
    def _median(values):
        ordered = sorted(values)
        count = len(ordered)
        if not count:
            return 0
        middle = count // 2
        if count % 2:
            return ordered[middle]
        return (ordered[middle - 1] + ordered[middle]) / 2

    def apply_quota_window_usage(self, stats):
        for stat in stats.values():
            windows = (stat.get("quota") or {}).get("windows") or {}
            source_window = "last_7d" if windows.get("last_7d") else "last_5h"
            bucket = windows.get(source_window) or {}
            reset_at = self.parse_detail_time_utc(bucket.get("reset_at"))
            window_seconds = int(bucket.get("limit_window_seconds") or 0)
            details = stat.get("_quota_usage_details") or []
            if not window_seconds:
                stat["quota_window"] = {"tokens": 0, "requests": 0}
                continue
            persisted = (stat.get("_quota_usage_windows") or {}).get(source_window)
            if isinstance(persisted, dict):
                observed_start = self.parse_detail_time_utc(persisted.get("observed_start_at"))
                observed_end = self.parse_detail_time_utc(persisted.get("observed_end_at"))
                end = reset_at or observed_end or datetime.utcnow()
                start = end - timedelta(seconds=window_seconds)
                observed_seconds = (observed_end - observed_start).total_seconds() if observed_start and observed_end else 0
                stat["quota_window"] = {
                    "tokens": int(persisted.get("tokens", 0) or 0),
                    "requests": int(persisted.get("requests", 0) or 0),
                    "start_at": start.isoformat() + "Z",
                    "reset_at": end.isoformat() + "Z",
                    "observed_start_at": observed_start.isoformat() + "Z" if observed_start else "",
                    "observed_end_at": observed_end.isoformat() + "Z" if observed_end else "",
                    "observed_seconds": observed_seconds,
                    "source_window": source_window,
                }
                continue
            if reset_at:
                end = reset_at
            else:
                end = max((when for when, _ in details), default=datetime.utcnow())
            start = end - timedelta(seconds=window_seconds)
            window_details = [(when, tokens) for when, tokens in details if start <= when <= end]
            observed_start = min((when for when, _ in window_details), default=None)
            observed_end = max((when for when, _ in window_details), default=None)
            observed_seconds = (observed_end - observed_start).total_seconds() if observed_start and observed_end else 0
            stat["quota_window"] = {
                "tokens": sum(tokens for _, tokens in window_details),
                "requests": len(window_details),
                "start_at": start.isoformat() + "Z",
                "reset_at": end.isoformat() + "Z",
                "observed_start_at": observed_start.isoformat() + "Z" if observed_start else "",
                "observed_end_at": observed_end.isoformat() + "Z" if observed_end else "",
                "observed_seconds": observed_seconds,
                "source_window": source_window,
            }

    def build_today_quota_usage(self, stats, today, today_used_tokens=None):
        account_count = len(stats)
        if today_used_tokens is None:
            today_used_tokens = sum(int((stat.get("today") or {}).get("tokens", 0) or 0) for stat in stats.values())
        else:
            today_used_tokens = int(today_used_tokens or 0)
        candidates = []
        source_windows = set()
        candidate_daily_limits = []
        candidate_window_limits = []
        quota_snapshot_count = 0
        quota_window_usage_count = 0
        partial_history_count = 0
        for stat in stats.values():
            quota_window = stat.get("quota_window") or {}
            quota_window_tokens = int(quota_window.get("tokens", 0) or 0)
            windows = (stat.get("quota") or {}).get("windows") or {}
            bucket = windows.get("last_7d") or windows.get("last_5h") or {}
            if bucket:
                quota_snapshot_count += 1
            if quota_window_tokens > 0:
                quota_window_usage_count += 1
            used_percent = self._number_or_none(bucket.get("used_percent"))
            window_seconds = self._number_or_none(bucket.get("limit_window_seconds")) or 0
            observed_seconds = self._number_or_none(quota_window.get("observed_seconds")) or 0
            coverage_ratio = observed_seconds / window_seconds if window_seconds else 0
            # Provider quota percentages describe the whole native window (often 7d),
            # but v7.1.29 local queue history starts only when Key Portal consumes it.
            # Do not infer full-account limits from a partial local history window.
            if quota_window_tokens > 0 and window_seconds > 0 and coverage_ratio < 0.8:
                partial_history_count += 1
            if quota_window_tokens <= 0 or used_percent is None or used_percent < 1 or window_seconds <= 0 or coverage_ratio < 0.8:
                continue
            window_limit = quota_window_tokens / (used_percent / 100)
            daily_limit = window_limit * 86400 / window_seconds
            if math.isfinite(daily_limit) and daily_limit > 0:
                candidate_daily_limits.append(daily_limit)
            if math.isfinite(window_limit) and window_limit > 0:
                candidate_window_limits.append(window_limit)
                candidates.append(window_limit)
                source_windows.add(quota_window.get("source_window") or ("last_7d" if windows.get("last_7d") else "last_5h"))
        single_account_window_limit = int(round(self._median(candidate_window_limits))) if candidate_window_limits else 0
        single_account_daily_limit = int(round(self._median(candidate_daily_limits))) if candidate_daily_limits else 0
        min_inferred_samples = min(account_count, max(3, math.ceil(account_count * 0.2))) if account_count else 0
        has_enough_samples = len(candidates) >= min_inferred_samples if min_inferred_samples else False
        total_daily_limit = account_count * single_account_daily_limit if has_enough_samples and single_account_daily_limit else 0
        usage_ratio = round(today_used_tokens / total_daily_limit, 6) if total_daily_limit else 0
        source = "unavailable"
        inference_status = "success" if candidates else "missing_quota_snapshot"
        if candidates:
            source = "inferred_from_provider_7d_window" if source_windows == {"last_7d"} else "inferred_from_provider_quota_window"
            if not has_enough_samples:
                inference_status = "insufficient_sample_size"
        elif partial_history_count:
            inference_status = "insufficient_history_coverage"
        elif quota_snapshot_count and not quota_window_usage_count:
            inference_status = "missing_quota_window_usage"
        elif quota_snapshot_count:
            inference_status = "insufficient_quota_window_percent"
        return {
            "date": today,
            "today_used_tokens": today_used_tokens,
            "account_count": account_count,
            "account_count_source": "auth_files",
            "single_account_daily_token_limit": single_account_daily_limit,
            "single_account_daily_token_limit_source": source,
            "single_account_window_token_limit": single_account_window_limit,
            "single_account_calendar_daily_token_limit": single_account_daily_limit,
            "total_daily_token_limit": total_daily_limit,
            "min_inferred_samples": min_inferred_samples,
            "usage_ratio": usage_ratio,
            "usage_percent": round(usage_ratio * 100, 2) if total_daily_limit else 0,
            "inferred_account_count": len(candidates),
            "inferred_daily_token_limit_min": int(round(min(candidate_daily_limits))) if candidate_daily_limits else 0,
            "inferred_daily_token_limit_max": int(round(max(candidate_daily_limits))) if candidate_daily_limits else 0,
            "configured": bool(total_daily_limit),
            "inferred_source_windows": sorted(source_windows),
            "quota_snapshot_count": quota_snapshot_count,
            "quota_window_usage_count": quota_window_usage_count,
            "partial_history_count": partial_history_count,
            "inference_status": inference_status,
        }

    def _refresh_today_quota_usage_from_summary(self, result):
        quota_usage = result.get("today_quota_usage")
        if not isinstance(quota_usage, dict):
            return
        try:
            summary, err = self.usage_summary_loader()
        except Exception:
            return
        if err or not summary:
            return
        today = summary.get("today") or quota_usage.get("date")
        today_used_tokens = int(summary.get("today_tokens", 0) or 0)
        total_daily_limit = int(quota_usage.get("total_daily_token_limit", 0) or 0)
        usage_ratio = round(today_used_tokens / total_daily_limit, 6) if total_daily_limit else 0
        updated = dict(quota_usage)
        updated["date"] = today
        updated["today_used_tokens"] = today_used_tokens
        updated["usage_ratio"] = usage_ratio
        updated["usage_percent"] = round(usage_ratio * 100, 2) if total_daily_limit else 0
        updated["today_used_tokens_source"] = "usage_summary_live"
        result["today_quota_usage"] = updated

    def _with_cache_metadata(self, data, now=None, refreshing=None):
        now = now or time.time()
        result = dict(data)
        self._refresh_today_quota_usage_from_summary(result)
        result["cache_ttl_seconds"] = self.stats_cache["ttl"]
        result["cache_age_seconds"] = round(now - self.stats_cache["last_update"], 3)
        result["refreshing"] = self.stats_cache["refreshing"] if refreshing is None else refreshing
        return result

    def refresh_cache(self):
        try:
            data = self.build()
            now = time.time()
            with self.stats_cache_lock:
                self.stats_cache["data"] = data
                self.stats_cache["last_update"] = now
        finally:
            with self.stats_cache_lock:
                self.stats_cache["refreshing"] = False

    def clear_cache(self):
        with self.stats_cache_lock:
            self.stats_cache["data"] = None
            self.stats_cache["last_update"] = 0
            self.stats_cache["refreshing"] = False

    def start_refresh(self):
        with self.stats_cache_lock:
            if self.stats_cache["refreshing"]:
                return False
            self.stats_cache["refreshing"] = True
        threading.Thread(target=self.refresh_cache, daemon=True).start()
        return True

    def get_cached(self):
        now = time.time()
        with self.stats_cache_lock:
            cached = self.stats_cache["data"]
            last_update = self.stats_cache["last_update"]
            ttl = self.stats_cache["ttl"]
            refreshing = self.stats_cache["refreshing"]
            if cached and now - last_update < ttl:
                return self._with_cache_metadata(cached, now, refreshing)
        data = self.build()
        with self.stats_cache_lock:
            self.stats_cache["data"] = data
            self.stats_cache["last_update"] = time.time()
            self.stats_cache["refreshing"] = False
        return self._with_cache_metadata(data, self.stats_cache["last_update"], False)

    def build(self):
        usage_snapshot = None
        if self.usage_snapshot_loader:
            try:
                usage_snapshot = self.usage_snapshot_loader()
            except Exception as exc:
                usage_snapshot = {
                    "auth_files": [],
                    "errors": [{"node": "cluster", "error": f"auth usage snapshot unavailable: {exc}"}],
                    "coverage_seconds": 0,
                }
            usage_payload = {"usage": {}, "node_errors": []}
        else:
            try:
                usage_payload = self.get_cluster_usage()
            except Exception:
                usage_payload = None
            if not usage_payload:
                usage_payload = {"usage": {}, "node_errors": [{"node": "cluster", "error": "usage unavailable"}]}
        files, auth_errors = self.get_cluster_auth_files()
        quota_errors = self.attach_proxy_quota_snapshots(files)
        now = datetime.utcnow()
        today = datetime.now().strftime("%Y-%m-%d")
        node_by_name = {node["name"]: node for node in self.nodes}
        windows = {
            "last_1h": now - timedelta(hours=1),
            "last_5h": now - timedelta(hours=5),
            "last_24h": now - timedelta(hours=24),
            "last_7d": now - timedelta(days=7),
            "total": None,
        }
        window_names = tuple(windows.keys())
        history_ranges = {
            "24h": ("hour", 24, now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=23)),
            "7d": ("day", 7, now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=6)),
            "30d": ("day", 30, now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=29)),
        }

        stats = {}
        def empty_window():
            return {
                "requests": 0,
                "success": 0,
                "failure": 0,
                "tokens": 0,
                "input_tokens": 0,
                "output_tokens": 0,
                "cached_tokens": 0,
                "reasoning_tokens": 0,
                "failure_rate": 0,
                "success_rate": 0,
                "avg_tokens_per_request": 0,
            }

        def empty_history():
            result = {}
            for name, (unit, count, start_time) in history_ranges.items():
                result[name] = [
                    {
                        "time": (start_time + (timedelta(hours=i) if unit == "hour" else timedelta(days=i))).isoformat() + "Z",
                        "requests": 0,
                        "success": 0,
                        "failure": 0,
                    }
                    for i in range(count)
                ]
            return result

        def add_history(history, when_value, failed):
            if not when_value:
                return
            for name, (unit, count, start_time) in history_ranges.items():
                if when_value < start_time:
                    continue
                normalized = when_value.replace(minute=0, second=0, microsecond=0) if unit == "hour" else when_value.replace(hour=0, minute=0, second=0, microsecond=0)
                seconds = (normalized - start_time).total_seconds()
                index = int(seconds // (3600 if unit == "hour" else 86400))
                if index < 0 or index >= count:
                    continue
                bucket = history[name][index]
                bucket["requests"] += 1
                if failed:
                    bucket["failure"] += 1
                else:
                    bucket["success"] += 1

        def enrich_window(bucket):
            requests_count = bucket.get("requests", 0) or 0
            if requests_count:
                bucket["failure_rate"] = round((bucket.get("failure", 0) or 0) * 100 / requests_count, 2)
                bucket["success_rate"] = round((bucket.get("success", 0) or 0) * 100 / requests_count, 2)
                bucket["avg_tokens_per_request"] = round((bucket.get("tokens", 0) or 0) / requests_count, 2)
            else:
                bucket["failure_rate"] = 0
                bucket["success_rate"] = 0
                bucket["avg_tokens_per_request"] = 0
            breakdown = self.build_token_breakdown(
                bucket.get("tokens", 0),
                bucket.get("input_tokens", 0),
                bucket.get("output_tokens", 0),
                bucket.get("cached_tokens", 0),
                bucket.get("reasoning_tokens", 0),
            )
            bucket["token_breakdown"] = breakdown
            bucket["estimated_cost_usd"] = breakdown["cost_usd"]
            return bucket

        def detail_error_message(detail):
            for key in ("error", "error_message", "message", "status_message", "reason"):
                value = detail.get(key)
                if isinstance(value, dict):
                    value = value.get("message") or value.get("error") or json.dumps(value, ensure_ascii=False)
                if value:
                    return str(value)
            return "请求失败"

        def detail_error_status(detail):
            for key in ("status", "status_code", "http_status", "code"):
                value = detail.get(key)
                if value:
                    return str(value)
            return ""

        def status_explanation(stat):
            if stat.get("disabled"):
                return "认证文件已禁用，不参与调度。"
            if stat.get("unavailable"):
                return "认证文件被标记为不可用，需要人工处理。"
            if stat.get("status") == "error":
                msg = stat.get("last_error_message") or stat.get("status_message") or "最近有失败记录。"
                return f"最近有失败记录：{msg}"
            if stat.get("last_5h", {}).get("failure", 0):
                return "最近 5 小时有失败请求，但账号未被标记为不可用。"
            if stat.get("last_5h", {}).get("requests", 0):
                return "最近 5 小时有成功请求。"
            return "当前未发现不可用标记。"

        auth_index_map = {}
        auth_index_node_map = {}
        account_map = {}
        account_node_map = {}
        for f in files:
            key = f.get("auth_index") or f.get("account") or f.get("email") or f.get("id") or f.get("name")
            if not key:
                continue
            node = f.get("node", "")
            account = f.get("account") or f.get("email") or f.get("label") or f.get("name") or key
            stats_key = f"{node}:{key}"
            stats[stats_key] = {
                "node": node,
                "account": account,
                "auth_id": f.get("id", ""),
                "auth_name": f.get("name", ""),
                "auth_index": f.get("auth_index", ""),
                "provider": f.get("provider") or f.get("type", ""),
                "plan_type": (f.get("id_token") or {}).get("plan_type", ""),
                "status": f.get("status", ""),
                "status_message": f.get("status_message", ""),
                "unavailable": bool(f.get("unavailable", False)),
                "disabled": bool(f.get("disabled", False)),
                "updated_at": f.get("updated_at") or f.get("modtime", ""),
                "last_request_at": "",
                "last_error_at": "",
                "last_error_message": "",
                "last_error_status": "",
                "today": {
                    "requests": 0,
                    "success": 0,
                    "failure": 0,
                    "tokens": 0,
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cached_tokens": 0,
                    "reasoning_tokens": 0,
                },
                "quota_window": {
                    "tokens": 0,
                    "requests": 0,
                },
                "history": empty_history(),
                "_quota_usage_details": [],
                "_quota_usage_windows": {},
                "quota": self.quota_from_snapshot(f),
            }
            for window_name in window_names:
                stats[stats_key][window_name] = empty_window()
            if stats[stats_key].get("auth_index"):
                auth_index = stats[stats_key]["auth_index"]
                auth_index_map[auth_index] = stats[stats_key]
                auth_index_node_map[(node, auth_index)] = stats[stats_key]
            if account:
                account_map[account] = stats[stats_key]
                account_node_map[(node, account)] = stats[stats_key]

        def find_stat(detail):
            auth_index = detail.get("auth_index")
            source = detail.get("source")
            node = detail.get("node")
            if node and auth_index and (node, auth_index) in auth_index_node_map:
                return auth_index_node_map[(node, auth_index)]
            if node and source and (node, source) in account_node_map:
                return account_node_map[(node, source)]
            if node and source:
                for (account_node, account), stat in account_node_map.items():
                    if account_node == node and source in account:
                        return stat
            if auth_index and auth_index in auth_index_map:
                return auth_index_map[auth_index]
            if source and source in account_map:
                return account_map[source]
            for account, stat in account_map.items():
                if source and source in account:
                    return stat
            return None

        if usage_snapshot is not None:
            metric_names = (
                "requests", "success", "failure", "tokens", "input_tokens",
                "output_tokens", "cached_tokens", "reasoning_tokens",
            )
            for persisted in usage_snapshot.get("auth_files", []) or []:
                if not isinstance(persisted, dict):
                    continue
                stat = find_stat(persisted)
                if not stat:
                    continue
                for window_name in window_names:
                    source_bucket = persisted.get(window_name)
                    if not isinstance(source_bucket, dict):
                        continue
                    target_bucket = stat[window_name]
                    for metric in metric_names:
                        target_bucket[metric] = int(source_bucket.get(metric, 0) or 0)
                    for metadata_name in ("complete", "coverage_seconds", "observed_start_at", "observed_end_at"):
                        if metadata_name in source_bucket:
                            target_bucket[metadata_name] = source_bucket[metadata_name]
                today_bucket = persisted.get("today")
                if isinstance(today_bucket, dict):
                    for metric in metric_names:
                        stat["today"][metric] = int(today_bucket.get(metric, 0) or 0)
                if isinstance(persisted.get("history"), dict):
                    stat["history"] = persisted["history"]
                for metadata_name in ("last_request_at", "last_error_at", "last_error_message", "last_error_status"):
                    value = persisted.get(metadata_name)
                    if value:
                        stat[metadata_name] = value.isoformat() if isinstance(value, datetime) else value
                stat["_quota_usage_windows"] = {
                    name: persisted.get(name)
                    for name in ("last_5h", "last_7d")
                    if isinstance(persisted.get(name), dict)
                }
        else:
            for api_stats in (usage_payload.get("usage", {}).get("apis", {}) or {}).values():
                for model_stats in (api_stats.get("models", {}) or {}).values():
                    for detail in model_stats.get("details", []) or []:
                        if not isinstance(detail, dict):
                            continue
                        stat = find_stat(detail)
                        if not stat:
                            continue
                        when = self.parse_detail_time(detail.get("timestamp"))
                        when_utc = self.parse_detail_time_utc(detail.get("timestamp"))
                        tokens_info = detail.get("tokens") or {}
                        tokens = int(tokens_info.get("total_tokens", 0) or 0)
                        input_tokens = int(tokens_info.get("input_tokens", 0) or 0)
                        output_tokens = int(tokens_info.get("output_tokens", 0) or 0)
                        cached_tokens = int(tokens_info.get("cached_tokens", 0) or 0)
                        reasoning_tokens = int(tokens_info.get("reasoning_tokens", 0) or 0)
                        failed = bool(detail.get("failed", False))
                        add_history(stat["history"], when, failed)
                        if when_utc:
                            stat["_quota_usage_details"].append((when_utc, tokens))
                        detail_timestamp = str(detail.get("timestamp") or "")
                        detail_date = detail_timestamp[:10] if re.match(r"^\d{4}-\d{2}-\d{2}", detail_timestamp) else None
                        if detail_date == today:
                            stat["today"]["requests"] += 1
                            stat["today"]["tokens"] += tokens
                            stat["today"]["input_tokens"] += input_tokens
                            stat["today"]["output_tokens"] += output_tokens
                            stat["today"]["cached_tokens"] += cached_tokens
                            stat["today"]["reasoning_tokens"] += reasoning_tokens
                            if failed:
                                stat["today"]["failure"] += 1
                            else:
                                stat["today"]["success"] += 1
                        for window_name, start_time in windows.items():
                            if start_time is not None and (when is None or when < start_time):
                                continue
                            bucket = stat[window_name]
                            bucket["requests"] += 1
                            bucket["tokens"] += tokens
                            bucket["input_tokens"] += input_tokens
                            bucket["output_tokens"] += output_tokens
                            bucket["cached_tokens"] += cached_tokens
                            bucket["reasoning_tokens"] += reasoning_tokens
                            if failed:
                                bucket["failure"] += 1
                            else:
                                bucket["success"] += 1
                        if when and (not stat["last_request_at"] or when > self.parse_detail_time(stat["last_request_at"])):
                            stat["last_request_at"] = detail.get("timestamp", "")
                        if failed and when and (not stat["last_error_at"] or when > self.parse_detail_time(stat["last_error_at"])):
                            stat["last_error_at"] = detail.get("timestamp", "")
                            stat["last_error_message"] = detail_error_message(detail)
                            stat["last_error_status"] = detail_error_status(detail)

        node_summary = {
            node["name"]: {
                "auth_files": 0,
                "active": 0,
                "warning": 0,
                "unavailable": 0,
                "history": empty_history(),
                "management_path": self.node_management_path(node.get("name", ""), index),
                "management_url": node.get("url", ""),
            }
            for index, node in enumerate(self.nodes)
        }
        for summary in node_summary.values():
            for window_name in window_names:
                summary.setdefault(window_name, empty_window())

        for stat in stats.values():
            for window_name in window_names:
                enrich_window(stat[window_name])
            enrich_window(stat["today"])
            stat["status_explanation"] = status_explanation(stat)

            node = stat["node"] or "unknown"
            summary = node_summary.setdefault(node, {
                "auth_files": 0,
                "active": 0,
                "warning": 0,
                "unavailable": 0,
                "history": empty_history(),
                "management_path": self.node_management_path(node, len(node_summary)),
                "management_url": "",
            })
            for window_name in window_names:
                summary.setdefault(window_name, empty_window())
            summary["auth_files"] += 1
            if stat.get("disabled") or stat.get("unavailable"):
                summary["unavailable"] += 1
            else:
                summary["active"] += 1
            if stat.get("status") == "error" and not (stat.get("disabled") or stat.get("unavailable")):
                summary["warning"] += 1
            for window_name in window_names:
                for metric in ("requests", "success", "failure", "tokens", "input_tokens", "output_tokens", "cached_tokens", "reasoning_tokens"):
                    summary[window_name][metric] += stat[window_name][metric]
            for range_name, buckets in (stat.get("history") or {}).items():
                summary_buckets = summary.get("history", {}).get(range_name, [])
                for index, bucket in enumerate(buckets or []):
                    if index >= len(summary_buckets):
                        break
                    summary_buckets[index]["requests"] += bucket.get("requests", 0)
                    summary_buckets[index]["success"] += bucket.get("success", 0)
                    summary_buckets[index]["failure"] += bucket.get("failure", 0)

        for node_name, summary in node_summary.items():
            for window_name in window_names:
                enrich_window(summary[window_name])
                if usage_snapshot is not None and window_name != "total":
                    coverage_by_node = usage_snapshot.get("coverage_by_node") or {}
                    coverage_seconds = int(coverage_by_node.get(node_name, usage_snapshot.get("coverage_seconds", 0)) or 0)
                    required_seconds = int((now - windows[window_name]).total_seconds())
                    summary[window_name]["complete"] = coverage_seconds >= required_seconds
                    summary[window_name]["coverage_seconds"] = min(coverage_seconds, required_seconds)

        self.apply_quota_window_usage(stats)
        if usage_snapshot is not None:
            today_used_tokens = sum(int((stat.get("today") or {}).get("tokens", 0) or 0) for stat in stats.values())
        else:
            today_used_tokens = int(((usage_payload.get("usage") or {}).get("tokens_by_day") or {}).get(today, 0) or 0)
        today_quota_usage = self.build_today_quota_usage(stats, today, today_used_tokens)
        for stat in stats.values():
            stat.pop("_quota_usage_details", None)
            stat.pop("_quota_usage_windows", None)

        return {
            "auth_files": sorted(stats.values(), key=lambda x: (x["node"], x["account"])),
            "nodes": node_summary,
            "configured_nodes": self.configured_nodes(),
            "today_quota_usage": today_quota_usage,
            "errors": (usage_snapshot.get("errors", []) if usage_snapshot is not None else usage_payload.get("node_errors", [])) + auth_errors + quota_errors,
            "coverage_seconds": int((usage_snapshot or {}).get("coverage_seconds", 0) or 0),
            "continuous_since": (usage_snapshot or {}).get("continuous_since", ""),
            "generated_at": (usage_snapshot or {}).get("generated_at") or datetime.utcnow().isoformat() + "Z",
        }
