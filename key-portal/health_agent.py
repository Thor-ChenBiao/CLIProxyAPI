#!/usr/bin/env python3
"""NLB-facing business health check for a CLIProxyAPI node."""

import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

try:
    import config
except Exception:
    config = None


def _env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


HOST = os.environ.get("HEALTH_AGENT_HOST", "127.0.0.1").strip() or "127.0.0.1"
PORT = _env_int("HEALTH_AGENT_PORT", 18081)
NODE_NAME = os.environ.get("NODE_NAME", os.uname().nodename).strip() or os.uname().nodename
CLIPROXY_API_URL = os.environ.get("CLIPROXY_API_URL", "http://127.0.0.1:8317").strip().rstrip("/")
LITELLM_URL = os.environ.get("LITELLM_URL", "http://127.0.0.1:4000").strip().rstrip("/")
MANAGEMENT_KEY = os.environ.get("CLIPROXY_MANAGEMENT_KEY", "").strip()
if not MANAGEMENT_KEY and config is not None:
    MANAGEMENT_KEY = getattr(config, "CLIPROXY_MANAGEMENT_KEY", "")
MIN_USABLE_AUTH_FILES = _env_int("HEALTH_AGENT_MIN_USABLE_AUTH_FILES", 1)
REQUIRE_PROVIDERS = [
    item.strip().lower()
    for item in os.environ.get("HEALTH_AGENT_REQUIRE_PROVIDERS", "").split(",")
    if item.strip()
]
REQUEST_TIMEOUT_SECONDS = _env_int("HEALTH_AGENT_TIMEOUT_SECONDS", 5)


def _get_json(path):
    request = Request(f"{CLIPROXY_API_URL}{path}")
    if MANAGEMENT_KEY:
        request.add_header("X-Management-Key", MANAGEMENT_KEY)
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        body = response.read()
        if not body:
            return {}
        return json.loads(body.decode("utf-8"))


def _get_status(path):
    request = Request(f"{CLIPROXY_API_URL}{path}")
    if MANAGEMENT_KEY:
        request.add_header("X-Management-Key", MANAGEMENT_KEY)
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        response.read()
        return response.status


def _auth_provider(item):
    return str(item.get("provider") or item.get("type") or "").strip().lower()


def _is_usable_auth_file(item):
    if not isinstance(item, dict):
        return False
    if item.get("disabled") is True:
        return False
    status = str(item.get("status") or "").strip().lower()
    if status in {"disabled", "expired", "invalid", "revoked"}:
        return False
    if item.get("unavailable") is True:
        return False
    return True


def evaluate_health():
    checks = {
        "node": NODE_NAME,
        "cliproxy_url": CLIPROXY_API_URL,
        "min_usable_auth_files": MIN_USABLE_AUTH_FILES,
        "required_providers": REQUIRE_PROVIDERS,
    }

    try:
        checks["cliproxy_health_status"] = _get_status("/healthz")
    except HTTPError as exc:
        checks["cliproxy_health_status"] = exc.code
        return False, "cliproxy health check failed", checks
    except (URLError, TimeoutError, OSError) as exc:
        checks["cliproxy_error"] = str(exc)
        return False, "cliproxy unreachable", checks

    if checks["cliproxy_health_status"] != 200:
        return False, "cliproxy health check failed", checks

    try:
        req = Request(f"{LITELLM_URL}/health/liveliness")
        with urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            checks["litellm_health_status"] = resp.status
    except HTTPError as exc:
        checks["litellm_health_status"] = exc.code
        return False, "litellm health check failed", checks
    except (URLError, TimeoutError, OSError) as exc:
        checks["litellm_error"] = str(exc)
        return False, "litellm unreachable", checks

    if checks.get("litellm_health_status") != 200:
        return False, "litellm health check failed", checks

    try:
        payload = _get_json("/v0/management/auth-files")
    except HTTPError as exc:
        checks["management_status"] = exc.code
        return False, "management auth-files check failed", checks
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        checks["management_error"] = str(exc)
        return False, "management auth-files unavailable", checks

    files = payload.get("files", []) if isinstance(payload, dict) else []
    if not isinstance(files, list):
        files = []

    usable_files = [item for item in files if _is_usable_auth_file(item)]
    provider_counts = {}
    usable_provider_counts = {}
    for item in files:
        provider = _auth_provider(item) or "unknown"
        provider_counts[provider] = provider_counts.get(provider, 0) + 1
    for item in usable_files:
        provider = _auth_provider(item) or "unknown"
        usable_provider_counts[provider] = usable_provider_counts.get(provider, 0) + 1

    checks.update({
        "management_status": 200,
        "auth_files_total": len(files),
        "auth_files_usable": len(usable_files),
        "auth_files_by_provider": provider_counts,
        "usable_auth_files_by_provider": usable_provider_counts,
    })

    if len(usable_files) < MIN_USABLE_AUTH_FILES:
        return False, "not enough usable auth files", checks

    missing_providers = [
        provider for provider in REQUIRE_PROVIDERS
        if usable_provider_counts.get(provider, 0) < 1
    ]
    if missing_providers:
        checks["missing_required_providers"] = missing_providers
        return False, "missing required auth providers", checks

    return True, "ok", checks


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.split("?", 1)[0] not in {"/healthz", "/readyz"}:
            self.send_response(404)
            self.end_headers()
            return

        healthy, reason, checks = evaluate_health()
        body = json.dumps({
            "status": "healthy" if healthy else "unhealthy",
            "reason": reason,
            "checked_at": int(time.time()),
            "checks": checks,
        }, ensure_ascii=False, sort_keys=True).encode("utf-8")

        self.send_response(200 if healthy else 503)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stderr.write("[health-agent] " + fmt % args + "\n")


def main():
    server = ThreadingHTTPServer((HOST, PORT), HealthHandler)
    print(f"[health-agent] listening on {HOST}:{PORT}, node={NODE_NAME}, cliproxy={CLIPROXY_API_URL}")
    server.serve_forever()


if __name__ == "__main__":
    main()
