"""Cluster health monitor for NLB-backed CLIProxyAPI nodes."""

import json
import os
import ssl
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import feishu


_state = {}


def _env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


ENABLED = os.environ.get("NLB_MONITOR_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
INTERVAL_SECONDS = _env_int("NLB_MONITOR_INTERVAL_SECONDS", 60)
FAIL_THRESHOLD = _env_int("NLB_MONITOR_FAIL_THRESHOLD", 2)
RECOVERY_THRESHOLD = _env_int("NLB_MONITOR_RECOVERY_THRESHOLD", 2)
TIMEOUT_SECONDS = _env_int("NLB_MONITOR_TIMEOUT_SECONDS", 8)
NOTIFY_EMAILS = [
    item.strip()
    for item in os.environ.get("NLB_MONITOR_NOTIFY_EMAILS", "").split(",")
    if item.strip()
]


def _health_url(node):
    base = str(node.get("health_url") or node.get("url") or "").strip().rstrip("/")
    if not base:
        return ""
    return f"{base}/healthz"


def _should_verify_tls(url):
    if os.environ.get("NLB_MONITOR_VERIFY_TLS", "").strip().lower() in {"0", "false", "no", "off"}:
        return False
    parsed = urlparse(url)
    host = parsed.hostname or ""
    if parsed.scheme == "https" and (host.startswith("172.31.") or host in {"127.0.0.1", "localhost"}):
        return False
    return True


def check_node(node):
    url = _health_url(node)
    if not url:
        return {
            "ok": False,
            "status_code": 0,
            "reason": "missing node url",
            "payload": {},
        }

    request = Request(url, headers={"Accept": "application/json"})
    context = None if _should_verify_tls(url) else ssl._create_unverified_context()
    try:
        with urlopen(request, timeout=TIMEOUT_SECONDS, context=context) as response:
            body = response.read()
            payload = _parse_payload(body)
            return {
                "ok": 200 <= response.status < 300 and payload.get("status") != "unhealthy",
                "status_code": response.status,
                "reason": payload.get("reason") or "ok",
                "payload": payload,
                "url": url,
            }
    except HTTPError as exc:
        body = exc.read()
        payload = _parse_payload(body)
        return {
            "ok": False,
            "status_code": exc.code,
            "reason": payload.get("reason") or f"HTTP {exc.code}",
            "payload": payload,
            "url": url,
        }
    except (URLError, TimeoutError, OSError) as exc:
        return {
            "ok": False,
            "status_code": 0,
            "reason": str(exc),
            "payload": {},
            "url": url,
        }


def _parse_payload(body):
    if not body:
        return {}
    try:
        return json.loads(body.decode("utf-8"))
    except Exception:
        return {"raw": body[:500].decode("utf-8", errors="replace")}


def _notify(title, content):
    if not NOTIFY_EMAILS:
        print(f"[NLBMonitor] No NLB_MONITOR_NOTIFY_EMAILS configured. {title}: {content}")
        return
    for email in NOTIFY_EMAILS:
        feishu.send_feishu_notification(email, title, content)


def _format_content(node_name, result, status_label):
    payload = result.get("payload") or {}
    checks = payload.get("checks") if isinstance(payload, dict) else {}
    if not isinstance(checks, dict):
        checks = {}

    lines = [
        f"**节点**: {node_name}",
        f"**状态**: {status_label}",
        f"**原因**: {result.get('reason') or '-'}",
        f"**HTTP**: {result.get('status_code')}",
        f"**URL**: {result.get('url') or '-'}",
    ]
    if checks:
        lines.extend([
            f"**认证文件**: usable {checks.get('auth_files_usable', '-') } / total {checks.get('auth_files_total', '-')}",
            f"**可用 provider**: `{json.dumps(checks.get('usable_auth_files_by_provider', {}), ensure_ascii=False)}`",
        ])
    return "\n".join(lines)


def monitor_once(nodes):
    if not ENABLED:
        return []

    events = []
    now = time.time()
    for node in nodes:
        node_name = str(node.get("name") or node.get("url") or "unknown")
        result = check_node(node)
        current = _state.setdefault(node_name, {
            "status": "unknown",
            "fail_count": 0,
            "success_count": 0,
            "last_reason": "",
        })

        if result["ok"]:
            current["success_count"] += 1
            current["fail_count"] = 0
            if current["status"] == "unhealthy" and current["success_count"] >= RECOVERY_THRESHOLD:
                current["status"] = "healthy"
                current["last_reason"] = result.get("reason", "ok")
                current["changed_at"] = now
                title = f"CLIProxyAPI 节点恢复: {node_name}"
                content = _format_content(node_name, result, "healthy")
                _notify(title, content)
                events.append({"node": node_name, "status": "healthy", "reason": result.get("reason"), "result": result})
            elif current["status"] == "unknown" and current["success_count"] >= RECOVERY_THRESHOLD:
                current["status"] = "healthy"
                current["last_reason"] = result.get("reason", "ok")
                current["changed_at"] = now
                events.append({"node": node_name, "status": "healthy", "reason": result.get("reason"), "result": result, "initial": True})
        else:
            current["fail_count"] += 1
            current["success_count"] = 0
            current["last_reason"] = result.get("reason", "unknown")
            if current["status"] != "unhealthy" and current["fail_count"] >= FAIL_THRESHOLD:
                current["status"] = "unhealthy"
                current["changed_at"] = now
                title = f"CLIProxyAPI 节点不可用: {node_name}"
                content = _format_content(node_name, result, "unhealthy")
                _notify(title, content)
                events.append({"node": node_name, "status": "unhealthy", "reason": result.get("reason"), "result": result})

        current["last_checked_at"] = now
        current["last_status_code"] = result.get("status_code")

    return events


def get_state():
    return dict(_state)
