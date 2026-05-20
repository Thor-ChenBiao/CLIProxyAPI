#!/usr/bin/env python3
"""
CLIProxyAPI Key Portal
A web service for managing OAuth key contributions and monitoring key status.
"""

import csv
import json
import math
import os
import re
import requests
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from collections import defaultdict
from urllib.parse import urlparse, parse_qs
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO, emit
from apscheduler.schedulers.background import BackgroundScheduler

import config
import database as db
import usage_sync

# Import modular components
import snapshot
import user_keys
import feishu
from routes import pages, websocket

app = Flask(__name__)
app.config['SECRET_KEY'] = 'key-portal-secret'
socketio = SocketIO(app, cors_allowed_origins="*")

# Track last usage state for change detection
_last_usage_state = {"total_tokens": 0, "total_requests": 0}

# Load user mapping
USER_MAPPING_FILE = os.path.join(os.path.dirname(__file__), "user_mapping.json")

# Stats cache
_stats_cache = {
    "data": None,  # Full usage data from CLIProxyAPI
    "last_update": 0,  # timestamp
    "ttl": 3  # cache management usage calls; keep dashboard near-real-time
}

_usage_summary_cache = {
    "data": None,
    "last_update": 0,
    "ttl": 1
}
_usage_summary_cache_lock = threading.Lock()

_auth_stats_cache = {
    "data": None,
    "last_update": 0,
    "ttl": 15,
    "refreshing": False
}
_auth_stats_cache_lock = threading.Lock()

_auth_quota_cache = {
    "data": {},
    "ttl": 300,
    "error_ttl": 60,
}
_auth_quota_cache_lock = threading.Lock()

_persistent_floor_cache = {
    "data": None,
    "last_update": 0,
    "ttl": 5,
}
_persistent_floor_cache_lock = threading.Lock()

_usage_floor_delta_baseline = {
    "data": {},
}
_usage_floor_delta_baseline_lock = threading.Lock()

_user_key_timeseries_cache = {
    "data": {},
    "ttl": 5,
}
_user_key_timeseries_cache_lock = threading.Lock()

_full_usage_cache = {
    "data": None,
    "last_update": 0,
    "ttl": 10,
}
_full_usage_cache_lock = threading.Lock()

_recent_hours_cache = {
    "data": None,
    "last_update": 0,
    "ttl": 60,
    "refreshing": False,
}
_recent_hours_cache_lock = threading.Lock()

# User keys cache and file path
USER_KEYS_FILE = os.path.join(os.path.dirname(__file__), "data", "user_keys.json")
KEY_POOL_FILE = os.path.join(os.path.dirname(__file__), "data", "key_pool.json")
ALLOWED_MODEL_GROUPS = {"common", "claude", "gemini", "deepseek"}


def float_env(name, default):
    try:
        value = os.environ.get(name, "")
        return float(value) if value else default
    except Exception:
        return default


TOKEN_PRICING_USD_PER_1M = {
    "input": float_env("KEY_PORTAL_INPUT_USD_PER_1M", 5.0),
    "output": float_env("KEY_PORTAL_OUTPUT_USD_PER_1M", 30.0),
    "cached": float_env("KEY_PORTAL_CACHED_USD_PER_1M", 0.5),
    "reasoning": float_env("KEY_PORTAL_REASONING_USD_PER_1M", 0.0),
}
_user_keys_cache = {
    "data": None,
    "loaded": False
}


def _public_base_url() -> str:
    base = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if base:
        return base.rstrip("/")
    host = os.environ.get("PUBLIC_HOST", "").strip()
    if host:
        return f"http://{host}:8080"
    return "http://localhost:8080"


def _login_url() -> str:
    return f"{_public_base_url()}/login"


def _api_base_url() -> str:
    base = os.environ.get("PUBLIC_API_BASE_URL", "").strip()
    if base:
        return base.rstrip("/")
    return _public_base_url().replace(":8080", ":8317")


def load_user_mapping():
    """Load user to Feishu ID mapping."""
    try:
        with open(USER_MAPPING_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"users": []}


def get_feishu_id(claude_email):
    """Get Feishu ID (email) for a given Claude email."""
    mapping = load_user_mapping()
    for user in mapping.get("users", []):
        if user.get("claude_email", "").lower() == claude_email.lower():
            return user.get("feishu_email") or claude_email
    return claude_email  # Default to original email if no mapping found


def get_user_name(claude_email):
    """Get user name for a given Claude email."""
    mapping = load_user_mapping()
    for user in mapping.get("users", []):
        if user.get("claude_email", "").lower() == claude_email.lower():
            return user.get("name", claude_email)
    return claude_email


# ============================================================================
# User Keys Management Functions
# ============================================================================

def load_user_keys():
    """Load user keys database into memory cache."""
    if _user_keys_cache["loaded"] and _user_keys_cache["data"]:
        return _user_keys_cache["data"]

    if os.path.exists(USER_KEYS_FILE):
        try:
            with open(USER_KEYS_FILE, "r") as f:
                data = json.load(f)
                _user_keys_cache["data"] = data
                _user_keys_cache["loaded"] = True
                print(f"[UserKeys] Loaded {len(data.get('users', {}))} users, {len(data.get('keys', {}))} keys")
                return data
        except Exception as e:
            print(f"[UserKeys] Error loading: {e}")

    # Initialize empty structure
    data = {"version": "1.0", "users": {}, "keys": {}}
    _user_keys_cache["data"] = data
    _user_keys_cache["loaded"] = True
    return data


def save_user_keys(data):
    """Save user keys database to file."""
    try:
        os.makedirs(os.path.dirname(USER_KEYS_FILE), exist_ok=True)
        with open(USER_KEYS_FILE, "w") as f:
            json.dump(data, f, indent=2)
        _user_keys_cache["data"] = data
        print(f"[UserKeys] Saved {len(data.get('users', {}))} users")
        return True
    except Exception as e:
        print(f"[UserKeys] Error saving: {e}")
        return False


def load_key_pool():
    """Load key pool."""
    if os.path.exists(KEY_POOL_FILE):
        try:
            with open(KEY_POOL_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[KeyPool] Error loading: {e}")
    return {"unused": [], "assigned": {}}


def save_key_pool(data):
    """Save key pool."""
    try:
        os.makedirs(os.path.dirname(KEY_POOL_FILE), exist_ok=True)
        with open(KEY_POOL_FILE, "w") as f:
            json.dump(data, f, indent=2)
        return True
    except Exception as e:
        print(f"[KeyPool] Error saving: {e}")
        return False


def normalize_model_group(model_group):
    model_group = str(model_group or "common").strip().lower()
    if model_group not in ALLOWED_MODEL_GROUPS:
        return "common"
    return model_group


def assign_key_to_user(email, name, label, model_group="common"):
    """Assign an unused key from pool to user."""
    model_group = normalize_model_group(model_group)

    # Load key pool
    pool = load_key_pool()

    if not pool.get("unused"):
        return None, "Key pool is empty, please generate more keys"

    # Take one key from pool
    api_key = pool["unused"].pop(0)
    pool["assigned"][api_key] = email
    save_key_pool(pool)

    # Load user keys database
    user_keys = load_user_keys()

    # Find or create user
    if email not in user_keys["users"]:
        user_keys["users"][email] = {
            "email": email,
            "name": name or email,
            "api_keys": [],
            "created_at": datetime.utcnow().isoformat() + "Z"
        }

    # Add key to user
    key_info = {
        "key": api_key,
        "label": label or "默认",
        "model_group": model_group,
        "created_at": datetime.utcnow().isoformat() + "Z",
        "last_used": None
    }
    user_keys["users"][email]["api_keys"].append(api_key)

    # Add to keys index
    user_keys["keys"][api_key] = {
        "email": email,
        "label": label or "默认",
        "model_group": model_group,
        "created_at": key_info["created_at"]
    }

    # Save
    save_user_keys(user_keys)

    print(f"[UserKeys] Assigned {api_key} to {email} ({model_group})")
    return api_key, None


def revoke_key(api_key):
    """Revoke a key (remove from user, add back to pool)."""
    user_keys = load_user_keys()

    # Find key owner
    key_info = user_keys["keys"].get(api_key)
    if not key_info:
        return False, "Key not found"

    email = key_info["email"]

    # Remove from user
    if email in user_keys["users"]:
        user_keys["users"][email]["api_keys"].remove(api_key)

    # Remove from keys index
    del user_keys["keys"][api_key]

    save_user_keys(user_keys)

    # Add back to pool
    pool = load_key_pool()
    pool["unused"].append(api_key)
    if api_key in pool["assigned"]:
        del pool["assigned"][api_key]
    save_key_pool(pool)

    # Remove from CLIProxyAPI
    data, err = call_management_api("GET", "/v0/management/api-keys")
    if not err:
        keys = data.get("api_keys", [])
        if api_key in keys:
            keys.remove(api_key)
            call_management_api("PUT", "/v0/management/api-keys", keys)

    print(f"[UserKeys] Revoked {api_key} from {email}")
    return True, None


def get_usage_stats_cached():
    """Get usage statistics with cache."""
    import time

    now = time.time()

    # Check cache
    if _stats_cache["data"] and (now - _stats_cache["last_update"]) < _stats_cache["ttl"]:
        return _stats_cache["data"], None

    # Fetch from all CLIProxyAPI nodes
    data = get_cluster_usage()

    # Cache stripped version (no per-request details) to save memory.
    _stats_cache["data"] = strip_usage_details(data)
    _stats_cache["last_update"] = now

    return _stats_cache["data"], None


def get_full_usage_cached():
    """Get full usage data (with per-request details) for timeseries."""
    now = time.time()
    with _full_usage_cache_lock:
        if _full_usage_cache["data"] and (now - _full_usage_cache["last_update"]) < _full_usage_cache["ttl"]:
            return _full_usage_cache["data"], None
    data = get_cluster_usage()
    with _full_usage_cache_lock:
        _full_usage_cache["data"] = data
        _full_usage_cache["last_update"] = time.time()
    return data, None


def is_valid_email(value):
    return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", (value or "").strip()))


def find_user_key_entries(user_keys_data, identifier):
    target = (identifier or "").strip().lower()
    if not target:
        return []

    users = user_keys_data.get("users", {})
    keys_index = user_keys_data.get("keys", {})
    user = users.get(identifier) or users.get(target)
    matched = {}

    if user:
        for api_key in user.get("api_keys", []):
            matched[api_key] = keys_index.get(api_key, {})

    for api_key, key_info in keys_index.items():
        owner = str(key_info.get("email", "")).strip().lower()
        label = str(key_info.get("label", "")).strip().lower()
        if owner == target or label == target:
            matched.setdefault(api_key, key_info)

    return [
        {"key": api_key, **(key_info or {})}
        for api_key, key_info in matched.items()
    ]


def get_user_stats(email, include_models=True):
    """Get statistics for a specific user (all their keys combined)."""
    user_keys_data = load_user_keys()
    user = user_keys_data["users"].get(email)
    key_entries = find_user_key_entries(user_keys_data, email)

    if not user and not key_entries:
        return None

    # Get stats
    stats_data, err = get_usage_stats_cached()
    if err:
        return {"email": email, "error": err}

    usage = stats_data.get("usage", {})
    apis = usage.get("apis", {})

    # Aggregate all keys for this user
    total_requests = 0
    total_tokens = 0
    keys_stats = []

    for key_meta in key_entries:
        api_key = key_meta.get("key", "")
        key_stats = apis.get(api_key, {})
        key_requests = key_stats.get("total_requests", 0)
        key_tokens = key_stats.get("total_tokens", 0)

        total_requests += key_requests
        total_tokens += key_tokens

        key_entry = {
            "key": api_key,
            "label": key_meta.get("label", ""),
            "total_requests": key_requests,
            "total_tokens": key_tokens,
        }
        if include_models:
            key_entry["models"] = key_stats.get("models", {})
        keys_stats.append(key_entry)

    return {
        "email": email,
        "name": (user or {}).get("name", email),
        "total_requests": total_requests,
        "total_tokens": total_tokens,
        "keys": keys_stats,
        "key_count": len(key_entries)
    }


def get_all_users_stats(include_models=True):
    """Get statistics for all users, sorted by token usage."""
    user_keys_data = load_user_keys()
    users = user_keys_data.get("users", {})

    all_stats = []
    for email in users:
        user_stat = get_user_stats(email, include_models=include_models)
        if user_stat:
            all_stats.append(user_stat)

    # Sort by total tokens descending
    all_stats.sort(key=lambda x: x.get("total_tokens", 0), reverse=True)

    return all_stats


def get_all_users_total_stats_from_db():
    user_keys_data = load_user_keys()
    users = user_keys_data.get("users", {})
    rows = db.get_all_users_total_usage()
    stats = []
    for row in rows:
        email = row.get("user_email", "")
        user_info = users.get(email, {})
        registered_keys = [
            entry.get("key", "")
            for entry in find_user_key_entries(user_keys_data, email)
            if entry.get("key", "")
        ]
        db_keys = row.get("api_keys", []) or []
        display_keys = registered_keys or db_keys
        breakdown = build_token_breakdown(row.get("total_tokens", 0), row.get("input_tokens", 0), row.get("output_tokens", 0), row.get("cached_tokens", 0), row.get("reasoning_tokens", 0))
        stats.append({
            "email": email,
            "name": user_info.get("name", email),
            "total_requests": row.get("total_requests", 0),
            "success_count": row.get("success_count", 0),
            "failure_count": row.get("failure_count", 0),
            "total_tokens": row.get("total_tokens", 0),
            "input_tokens": row.get("input_tokens", 0),
            "output_tokens": row.get("output_tokens", 0),
            "cached_tokens": row.get("cached_tokens", 0),
            "reasoning_tokens": row.get("reasoning_tokens", 0),
            "token_breakdown": breakdown,
            "estimated_cost_usd": breakdown["cost_usd"],
            "key_count": len(set(display_keys)) or row.get("num_keys", 0),
            "keys": [{"key": key} for key in display_keys],
        })
    return stats


def get_all_users_stats_by_period(period="month", live_today=False):
    """
    Get statistics for all users aggregated by period (month or year).
    Returns a list with each user's stats broken down by the selected period.
    """
    # Get stats from database
    stats = db.get_user_usage_by_period(period)

    # Load user names
    user_keys_data = load_user_keys()
    users = user_keys_data.get("users", {})

    today = beijing_today()
    live_apis = {}
    if live_today and period == "day":
        stats_data, _ = get_usage_stats_cached()
        live_apis = stats_data.get("usage", {}).get("apis", {}) if stats_data else {}

    # Enrich with user names and format output
    all_stats = []
    for stat in stats:
        email = stat['user_email']
        user_info = users.get(email, {})
        name = user_info.get("name", email)
        period_keys = [key for key in stat.get('api_keys', []) if key]
        registered_keys = [
            entry.get("key", "")
            for entry in find_user_key_entries(user_keys_data, email)
            if entry.get("key", "")
        ]
        display_keys = registered_keys or period_keys
        total_requests = stat['total_requests']
        total_tokens = stat['total_tokens']
        input_tokens = stat.get('input_tokens', 0)
        output_tokens = stat.get('output_tokens', 0)
        cached_tokens = stat.get('cached_tokens', 0)
        reasoning_tokens = stat.get('reasoning_tokens', 0)
        if live_today and period == "day" and stat['period'] == today:
            live_totals = [key_usage_for_date(live_apis.get(key, {}), today) for key in period_keys]
            total_requests = max(total_requests, sum(item.get("total_requests", 0) for item in live_totals))
            total_tokens = max(total_tokens, sum(item.get("total_tokens", 0) for item in live_totals))
            input_tokens = max(input_tokens, sum(item.get("input_tokens", 0) for item in live_totals))
            output_tokens = max(output_tokens, sum(item.get("output_tokens", 0) for item in live_totals))
            cached_tokens = max(cached_tokens, sum(item.get("cached_tokens", 0) for item in live_totals))
            reasoning_tokens = max(reasoning_tokens, sum(item.get("reasoning_tokens", 0) for item in live_totals))
        breakdown = build_token_breakdown(total_tokens, input_tokens, output_tokens, cached_tokens, reasoning_tokens)

        all_stats.append({
            "email": email,
            "name": name,
            "period": stat['period'],
            "total_requests": total_requests,
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "token_breakdown": breakdown,
            "estimated_cost_usd": breakdown["cost_usd"],
            "key_count": len(set(display_keys)),
            "_api_keys": display_keys,
        })

    return all_stats


# Cache for Feishu access token
_feishu_token_cache = {"token": None, "expires_at": 0}


def get_feishu_access_token():
    """Get Feishu tenant access token."""
    import time

    # Check cache
    if _feishu_token_cache["token"] and _feishu_token_cache["expires_at"] > time.time():
        return _feishu_token_cache["token"]

    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        return None

    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": config.FEISHU_APP_ID,
                "app_secret": config.FEISHU_APP_SECRET
            },
            timeout=10
        )
        data = resp.json()
        if data.get("code") == 0:
            token = data.get("tenant_access_token")
            expire = data.get("expire", 7200)
            _feishu_token_cache["token"] = token
            _feishu_token_cache["expires_at"] = time.time() + expire - 60  # 60s buffer
            return token
        else:
            print(f"[Feishu] Failed to get token: {data}")
            return None
    except Exception as e:
        print(f"[Feishu] Error getting token: {e}")
        return None


def send_feishu_notification(receiver_email, title, content):
    """Send notification via Feishu Open API to user by email."""
    token = get_feishu_access_token()
    if not token:
        print(f"[Feishu] No token available. Would notify {receiver_email}: {title}")
        return False

    try:
        # Send message to user by email
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "email"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json"
            },
            json={
                "receive_id": receiver_email,
                "msg_type": "interactive",
                "content": json.dumps({
                    "config": {"wide_screen_mode": True},
                    "header": {
                        "title": {"tag": "plain_text", "content": title},
                        "template": "orange"
                    },
                    "elements": [
                        {
                            "tag": "div",
                            "text": {"tag": "lark_md", "content": content}
                        },
                        {
                            "tag": "action",
                            "actions": [
                                {
                                    "tag": "button",
                                    "text": {"tag": "plain_text", "content": "重新授权"},
                                    "type": "primary",
                                    "url": _login_url()
                                }
                            ]
                        }
                    ]
                })
            },
            timeout=10
        )
        data = resp.json()
        if data.get("code") == 0:
            print(f"[Feishu] Sent notification to {receiver_email}")
            return True
        else:
            print(f"[Feishu] Failed to send to {receiver_email}: {data}")
            return False
    except Exception as e:
        print(f"[Feishu] Error sending notification: {e}")
        return False


# Snapshot file path
SNAPSHOT_FILE = os.path.join(os.path.dirname(__file__), "data", "cliproxy_snapshot.json")
SNAPSHOT_DIR = os.path.join(os.path.dirname(__file__), "data", "cliproxy_snapshots")

# State for restart detection
_cliproxy_state = {
    "last_total_tokens": 0,
    "last_total_requests": 0,
    "last_check_time": None,
    "restart_count": 0
}


def call_management_api(method, endpoint, data=None):
    """Call CLIProxyAPI management API."""
    url = f"{config.CLIPROXY_API_URL}{endpoint}"
    headers = {"X-Management-Key": config.CLIPROXY_MANAGEMENT_KEY}

    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=30)
        elif method == "POST":
            resp = requests.post(url, headers=headers, json=data, timeout=30)
        elif method == "PATCH":
            resp = requests.patch(url, headers=headers, json=data, timeout=30)
        else:
            return None, f"Unsupported method: {method}"

        if resp.status_code == 200:
            return resp.json(), None
        else:
            return None, f"API error: {resp.status_code} - {resp.text}"
    except Exception as e:
        return None, str(e)



DEFAULT_CLIPROXY_NODES = [
    {"name": "old", "url": "http://127.0.0.1:8317"},
    {"name": "node-b", "url": "http://172.31.26.28:8317"},
]


def load_cliproxy_nodes():
    raw = os.environ.get("CLIPROXY_NODES_JSON", "").strip()
    if raw:
        try:
            nodes = json.loads(raw)
            if isinstance(nodes, list):
                parsed = []
                for item in nodes:
                    if not isinstance(item, dict):
                        continue
                    name = str(item.get("name") or "").strip()
                    url = str(item.get("url") or "").strip().rstrip("/")
                    if name and url:
                        parsed.append({"name": name, "url": url})
                if parsed:
                    return parsed
        except Exception as e:
            print(f"[Cluster] Invalid CLIPROXY_NODES_JSON, using defaults: {e}")
    return [dict(node) for node in DEFAULT_CLIPROXY_NODES]


CLIPROXY_NODES = load_cliproxy_nodes()


def call_management_api_node(node, method, endpoint, data=None, timeout=30):
    """Call one CLIProxyAPI management endpoint."""
    url = f"{node['url']}{endpoint}"
    headers = {"X-Management-Key": config.CLIPROXY_MANAGEMENT_KEY}
    try:
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=timeout)
        elif method == "POST":
            resp = requests.post(url, headers=headers, json=data, timeout=timeout)
        elif method == "PATCH":
            resp = requests.patch(url, headers=headers, json=data, timeout=timeout)
        else:
            return None, f"Unsupported method: {method}"
        if resp.status_code == 200:
            return resp.json(), None
        return None, f"API error: {resp.status_code} - {resp.text}"
    except Exception as e:
        return None, str(e)


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


def _clamp_percent(value):
    if value is None:
        return None
    return max(0, min(100, round(float(value), 2)))


def _camel_or_snake(data, snake, camel=None):
    if not isinstance(data, dict):
        return None
    if snake in data:
        return data.get(snake)
    if camel and camel in data:
        return data.get(camel)
    return None


def _reset_at_iso(window):
    if not isinstance(window, dict):
        return ""
    reset_at = _number_or_none(
        _camel_or_snake(window, "reset_at", "resetAt")
        or _camel_or_snake(window, "reset_time", "resetTime")
    )
    if reset_at and reset_at > 0:
        try:
            return datetime.utcfromtimestamp(reset_at).isoformat() + "Z"
        except (OverflowError, OSError, ValueError):
            pass
    reset_after = _number_or_none(
        _camel_or_snake(window, "reset_after_seconds", "resetAfterSeconds")
        or _camel_or_snake(window, "reset_in", "resetIn")
    )
    if reset_after and reset_after > 0:
        return (datetime.utcnow() + timedelta(seconds=reset_after)).isoformat() + "Z"
    raw_reset = _camel_or_snake(window, "resets_at", "resetsAt")
    if isinstance(raw_reset, str) and raw_reset.strip():
        return raw_reset.strip()
    return ""


def _quota_window_from_used_percent(window, limit_label):
    if not isinstance(window, dict):
        return None
    used_raw = _camel_or_snake(window, "used_percent", "usedPercent")
    if used_raw is None:
        used_raw = window.get("utilization")
    used = _number_or_none(used_raw)
    if used is None and _reset_at_iso(window):
        used = 100
    if used is None:
        return None
    remaining = _clamp_percent(100 - used)
    return {
        "limit_label": limit_label,
        "used_percent": _clamp_percent(used),
        "remaining_percent": remaining,
        "reset_at": _reset_at_iso(window),
        "limit_window_seconds": _number_or_none(_camel_or_snake(window, "limit_window_seconds", "limitWindowSeconds")),
    }


def _parse_json_body(body):
    if isinstance(body, dict):
        return body
    if not isinstance(body, str):
        return None
    text = body.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _api_call_node(node, auth_index, method, url, headers, body=""):
    payload = {
        "auth_index": auth_index,
        "method": method,
        "url": url,
        "header": headers,
    }
    if body:
        payload["data"] = body
    data, err = call_management_api_node(node, "POST", "/v0/management/api-call", data=payload, timeout=30)
    if err:
        raise RuntimeError(err)
    status_code = int(data.get("status_code", 0) or 0)
    if status_code < 200 or status_code >= 300:
        response_body = str(data.get("body", "") or "").strip()
        if len(response_body) > 200:
            response_body = response_body[:200] + "..."
        raise RuntimeError(f"provider quota API returned {status_code}: {response_body}")
    return _parse_json_body(data.get("body"))


def _auth_chatgpt_account_id(auth_file):
    id_token = auth_file.get("id_token")
    if isinstance(id_token, dict):
        account_id = id_token.get("chatgpt_account_id") or id_token.get("chatgptAccountId")
        if isinstance(account_id, str) and account_id.strip():
            return account_id.strip()
    return ""


def _build_error_quota(provider, message):
    return {
        "status": "error",
        "provider": provider,
        "error": message,
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "windows": {},
    }


def _fetch_codex_quota(node, auth_file):
    auth_index = (auth_file.get("auth_index") or auth_file.get("authIndex") or "").strip()
    account_id = _auth_chatgpt_account_id(auth_file)
    if not auth_index:
        return _build_error_quota("codex", "auth_index missing")
    if not account_id:
        return _build_error_quota("codex", "chatgpt account id missing")
    payload = _api_call_node(node, auth_index, "GET", "https://chatgpt.com/backend-api/wham/usage", {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "User-Agent": "codex_cli_rs/0.76.0 (Debian 13.0.0; x86_64) WindowsTerminal",
        "Chatgpt-Account-Id": account_id,
    })
    if not payload:
        return _build_error_quota("codex", "empty quota response")
    rate_limit = payload.get("rate_limit") or payload.get("rateLimit") or {}
    primary = rate_limit.get("primary_window") or rate_limit.get("primaryWindow")
    secondary = rate_limit.get("secondary_window") or rate_limit.get("secondaryWindow")
    return {
        "status": "success",
        "provider": "codex",
        "plan_type": (payload.get("plan_type") or payload.get("planType") or auth_file.get("plan_type") or ""),
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "windows": {
            "last_5h": _quota_window_from_used_percent(primary, "5h 原生窗口"),
            "last_7d": _quota_window_from_used_percent(secondary, "7d 原生窗口"),
        },
    }


def _pick_claude_seven_day_window(payload):
    candidates = [
        ("seven_day", "7d 原生窗口"),
        ("seven_day_oauth_apps", "7d OAuth Apps"),
        ("seven_day_opus", "7d Opus"),
        ("seven_day_sonnet", "7d Sonnet"),
        ("seven_day_cowork", "7d Cowork"),
    ]
    parsed = []
    for key, label in candidates:
        item = _quota_window_from_used_percent(payload.get(key), label)
        if item:
            parsed.append(item)
    if not parsed:
        return None
    return sorted(parsed, key=lambda item: item.get("remaining_percent", 101))[0]


def _fetch_claude_quota(node, auth_file):
    auth_index = (auth_file.get("auth_index") or auth_file.get("authIndex") or "").strip()
    if not auth_index:
        return _build_error_quota("claude", "auth_index missing")
    payload = _api_call_node(node, auth_index, "GET", "https://api.anthropic.com/api/oauth/usage", {
        "Authorization": "Bearer $TOKEN$",
        "Content-Type": "application/json",
        "anthropic-beta": "oauth-2025-04-20",
    })
    if not payload:
        return _build_error_quota("claude", "empty quota response")
    return {
        "status": "success",
        "provider": "claude",
        "fetched_at": datetime.utcnow().isoformat() + "Z",
        "windows": {
            "last_5h": _quota_window_from_used_percent(payload.get("five_hour"), "5h 原生窗口"),
            "last_7d": _pick_claude_seven_day_window(payload),
        },
        "extra_usage": payload.get("extra_usage"),
    }


def fetch_auth_quota(node, auth_file):
    provider = str(auth_file.get("provider") or auth_file.get("type") or "").strip().lower()
    try:
        if provider == "codex":
            return _fetch_codex_quota(node, auth_file)
        if provider == "claude":
            return _fetch_claude_quota(node, auth_file)
        return {
            "status": "unsupported",
            "provider": provider,
            "fetched_at": datetime.utcnow().isoformat() + "Z",
            "windows": {},
        }
    except Exception as e:
        return _build_error_quota(provider, str(e))


def get_auth_quota_cached(node, auth_file):
    auth_index = (auth_file.get("auth_index") or auth_file.get("authIndex") or "").strip()
    provider = str(auth_file.get("provider") or auth_file.get("type") or "").strip().lower()
    if not auth_index:
        return _build_error_quota(provider, "auth_index missing")
    cache_key = f"{node.get('name', '')}:{auth_index}:{provider}"
    now = time.time()
    with _auth_quota_cache_lock:
        cached = _auth_quota_cache["data"].get(cache_key)
        if cached:
            ttl = _auth_quota_cache["error_ttl"] if cached.get("quota", {}).get("status") == "error" else _auth_quota_cache["ttl"]
            if (now - cached.get("updated_at", 0)) < ttl:
                return dict(cached["quota"], cache_age_seconds=round(now - cached.get("updated_at", now), 3))
    quota = fetch_auth_quota(node, auth_file)
    with _auth_quota_cache_lock:
        _auth_quota_cache["data"][cache_key] = {
            "updated_at": now,
            "quota": quota,
        }
    return dict(quota, cache_age_seconds=0)


def _median(values):
    ordered = sorted(values)
    count = len(ordered)
    if not count:
        return 0
    middle = count // 2
    if count % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def apply_quota_window_usage(stats):
    for stat in stats.values():
        bucket = (((stat.get("quota") or {}).get("windows") or {}).get("last_7d") or {})
        reset_at = parse_detail_time_utc(bucket.get("reset_at"))
        window_seconds = int(bucket.get("limit_window_seconds") or 0)
        details = stat.get("_quota_usage_details") or []
        if not reset_at or not window_seconds or not details:
            stat["quota_window"] = {"tokens": 0, "requests": 0}
            continue
        start = reset_at - timedelta(seconds=window_seconds)
        window_details = [(when, tokens) for when, tokens in details if start <= when <= reset_at]
        stat["quota_window"] = {
            "tokens": sum(tokens for _, tokens in window_details),
            "requests": len(window_details),
            "start_at": start.isoformat() + "Z",
            "reset_at": reset_at.isoformat() + "Z",
        }


def build_today_quota_usage(stats, today, today_used_tokens=None):
    account_count = len(stats)
    if today_used_tokens is None:
        today_used_tokens = sum(int((stat.get("today") or {}).get("tokens", 0) or 0) for stat in stats.values())
    else:
        today_used_tokens = int(today_used_tokens or 0)
    candidates = []
    for stat in stats.values():
        quota_window_tokens = int((stat.get("quota_window") or {}).get("tokens", 0) or 0)
        bucket = (((stat.get("quota") or {}).get("windows") or {}).get("last_7d") or {})
        used_percent = _number_or_none(bucket.get("used_percent"))
        if quota_window_tokens <= 0 or used_percent is None or used_percent < 1:
            continue
        daily_limit = quota_window_tokens / (used_percent / 100) / 7
        if math.isfinite(daily_limit) and daily_limit > 0:
            candidates.append(daily_limit)
    single_account_daily_limit = int(round(_median(candidates))) if candidates else 0
    total_daily_limit = account_count * single_account_daily_limit if single_account_daily_limit else 0
    usage_ratio = round(today_used_tokens / total_daily_limit, 6) if total_daily_limit else 0
    return {
        "date": today,
        "today_used_tokens": today_used_tokens,
        "account_count": account_count,
        "account_count_source": "auth_files",
        "single_account_daily_token_limit": single_account_daily_limit,
        "single_account_daily_token_limit_source": "inferred_from_provider_7d_window" if single_account_daily_limit else "unavailable",
        "total_daily_token_limit": total_daily_limit,
        "usage_ratio": usage_ratio,
        "usage_percent": round(usage_ratio * 100, 2) if total_daily_limit else 0,
        "inferred_account_count": len(candidates),
        "inferred_daily_token_limit_min": int(round(min(candidates))) if candidates else 0,
        "inferred_daily_token_limit_max": int(round(max(candidates))) if candidates else 0,
        "configured": bool(total_daily_limit),
    }


def merge_usage_payloads(results):
    """Merge /v0/management/usage results from all nodes."""
    # Only keep details from the last 7 days to cap memory usage.
    _cutoff_ts = (datetime.utcnow() - timedelta(days=2)).strftime("%Y-%m-%dT")
    merged_usage = {
        "total_requests": 0,
        "success_count": 0,
        "failure_count": 0,
        "total_tokens": 0,
        "apis": {},
        "tokens_by_day": defaultdict(int),
        "requests_by_day": defaultdict(int),
        "tokens_by_hour": defaultdict(int),
        "requests_by_hour": defaultdict(int),
        "success_by_hour": defaultdict(int),
        "failure_by_hour": defaultdict(int),
        "latency_sum_by_hour": defaultdict(int),
        "latency_count_by_hour": defaultdict(int),
    }
    node_summaries = []
    errors = []

    def add_int_dict(target, source):
        for key, value in (source or {}).items():
            try:
                target[key] += int(value or 0)
            except Exception:
                pass

    def detail_hour_key(detail):
        value = str(detail.get("timestamp") or "")
        if len(value) >= 13 and value[10] == "T" and value[11:13].isdigit():
            return value[11:13]
        return None

    for node_name, payload, err in results:
        if err:
            errors.append({"node": node_name, "error": err})
            continue
        usage = (payload or {}).get("usage", {})
        node_summaries.append({
            "node": node_name,
            "total_requests": usage.get("total_requests", 0),
            "success_count": usage.get("success_count", 0),
            "failure_count": usage.get("failure_count", 0),
            "total_tokens": usage.get("total_tokens", 0),
        })
        for key in ("total_requests", "success_count", "failure_count", "total_tokens"):
            merged_usage[key] += int(usage.get(key, 0) or 0)
        add_int_dict(merged_usage["tokens_by_day"], usage.get("tokens_by_day", {}))
        add_int_dict(merged_usage["requests_by_day"], usage.get("requests_by_day", {}))
        add_int_dict(merged_usage["tokens_by_hour"], usage.get("tokens_by_hour", {}))
        add_int_dict(merged_usage["requests_by_hour"], usage.get("requests_by_hour", {}))
        add_int_dict(merged_usage["success_by_hour"], usage.get("success_by_hour", {}))
        add_int_dict(merged_usage["failure_by_hour"], usage.get("failure_by_hour", {}))
        add_int_dict(merged_usage["latency_sum_by_hour"], usage.get("latency_sum_by_hour", {}))
        add_int_dict(merged_usage["latency_count_by_hour"], usage.get("latency_count_by_hour", {}))
        derive_success_failure_by_hour = not usage.get("success_by_hour") and not usage.get("failure_by_hour")
        derive_latency_by_hour = not usage.get("latency_sum_by_hour") and not usage.get("latency_count_by_hour")

        for api_key, api_stats in (usage.get("apis", {}) or {}).items():
            out_api = merged_usage["apis"].setdefault(api_key, {
                "total_requests": 0,
                "total_tokens": 0,
                "models": {},
                "nodes": {},
            })
            out_api["total_requests"] += int(api_stats.get("total_requests", 0) or 0)
            out_api["total_tokens"] += int(api_stats.get("total_tokens", 0) or 0)
            out_api["nodes"][node_name] = {
                "total_requests": api_stats.get("total_requests", 0),
                "total_tokens": api_stats.get("total_tokens", 0),
            }
            for model, model_stats in (api_stats.get("models", {}) or {}).items():
                out_model = out_api["models"].setdefault(model, {
                    "total_requests": 0,
                    "total_tokens": 0,
                    "details": [],
                })
                out_model["total_requests"] += int(model_stats.get("total_requests", 0) or 0)
                out_model["total_tokens"] += int(model_stats.get("total_tokens", 0) or 0)
                for detail in model_stats.get("details", []) or []:
                    if isinstance(detail, dict):
                        ts = str(detail.get("timestamp") or "")[:13]
                        if ts < _cutoff_ts:
                            continue
                        detail = dict(detail)
                        detail["node"] = node_name
                        detail["api_key"] = api_key
                        detail["model"] = model
                        out_model["details"].append(detail)
                        hour_key = detail_hour_key(detail)
                        if derive_success_failure_by_hour and hour_key:
                            if detail.get("failed"):
                                merged_usage["failure_by_hour"][hour_key] += 1
                            else:
                                merged_usage["success_by_hour"][hour_key] += 1
                        if derive_latency_by_hour and hour_key:
                            try:
                                latency_ms = int(detail.get("latency_ms") or 0)
                            except Exception:
                                latency_ms = 0
                            if latency_ms > 0:
                                merged_usage["latency_sum_by_hour"][hour_key] += latency_ms
                                merged_usage["latency_count_by_hour"][hour_key] += 1

    merged_usage["avg_latency_ms_by_hour"] = {
        hour: round(merged_usage["latency_sum_by_hour"][hour] / count, 2)
        for hour, count in merged_usage["latency_count_by_hour"].items()
        if count
    }
    for key in ("tokens_by_day", "requests_by_day", "tokens_by_hour", "requests_by_hour", "success_by_hour", "failure_by_hour", "latency_sum_by_hour", "latency_count_by_hour", "avg_latency_ms_by_hour"):
        merged_usage[key] = dict(merged_usage[key])
    return {"usage": merged_usage, "failed_requests": merged_usage["failure_count"], "nodes": node_summaries, "node_errors": errors}



def strip_usage_details(payload):
    """Return usage payload without high-cardinality model details for dashboard counters."""
    payload = json.loads(json.dumps(payload))
    for api_stats in (payload.get("usage", {}).get("apis", {}) or {}).values():
        for model_stats in (api_stats.get("models", {}) or {}).values():
            model_stats.pop("details", None)
    return payload


def _refresh_today_quota_usage_from_summary(result):
    quota_usage = result.get("today_quota_usage")
    if not isinstance(quota_usage, dict):
        return
    try:
        summary, err = get_usage_summary_cached()
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


def _with_auth_stats_cache_metadata(data, now=None, refreshing=None):
    import time
    now = now or time.time()
    result = dict(data)
    _refresh_today_quota_usage_from_summary(result)
    result["cache_ttl_seconds"] = _auth_stats_cache["ttl"]
    result["cache_age_seconds"] = round(now - _auth_stats_cache["last_update"], 3)
    result["refreshing"] = _auth_stats_cache["refreshing"] if refreshing is None else refreshing
    return result


def refresh_auth_stats_cache():
    import time
    try:
        data = build_auth_stats()
        now = time.time()
        with _auth_stats_cache_lock:
            _auth_stats_cache["data"] = data
            _auth_stats_cache["last_update"] = now
    finally:
        with _auth_stats_cache_lock:
            _auth_stats_cache["refreshing"] = False


def clear_auth_stats_cache():
    with _auth_stats_cache_lock:
        _auth_stats_cache["data"] = None
        _auth_stats_cache["last_update"] = 0
        _auth_stats_cache["refreshing"] = False


def start_auth_stats_refresh():
    with _auth_stats_cache_lock:
        if _auth_stats_cache["refreshing"]:
            return False
        _auth_stats_cache["refreshing"] = True
    threading.Thread(target=refresh_auth_stats_cache, daemon=True).start()
    return True


def get_auth_stats_cached():
    import time
    now = time.time()
    with _auth_stats_cache_lock:
        cached = _auth_stats_cache["data"]
        last_update = _auth_stats_cache["last_update"]
        ttl = _auth_stats_cache["ttl"]
        refreshing = _auth_stats_cache["refreshing"]
    if cached and (now - last_update) < ttl:
        return _with_auth_stats_cache_metadata(cached, now, refreshing)
    if cached:
        start_auth_stats_refresh()
        return _with_auth_stats_cache_metadata(cached, now, True)
    data = build_auth_stats()
    now = time.time()
    with _auth_stats_cache_lock:
        _auth_stats_cache["data"] = data
        _auth_stats_cache["last_update"] = now
        _auth_stats_cache["refreshing"] = False
    return _with_auth_stats_cache_metadata(data, now, False)

def call_management_api_all(method, endpoint, data=None, timeout=30):
    def fetch(node):
        payload, err = call_management_api_node(node, method, endpoint, data=data, timeout=timeout)
        if err:
            print(f"[Cluster] {node['name']} {endpoint} failed: {err}")
        return node["name"], payload, err

    workers = min(max(len(CLIPROXY_NODES), 1), 8)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_by_index = {
            executor.submit(fetch, node): index
            for index, node in enumerate(CLIPROXY_NODES)
        }
        results = [None] * len(CLIPROXY_NODES)
        for future in as_completed(future_by_index):
            index = future_by_index[future]
            node = CLIPROXY_NODES[index]
            try:
                results[index] = future.result()
            except Exception as e:
                results[index] = (node["name"], None, str(e))
                print(f"[Cluster] {node['name']} {endpoint} failed: {e}")
    return [result for result in results if result is not None]


def get_cluster_usage():
    return merge_usage_payloads(call_management_api_all("GET", "/v0/management/usage", timeout=30))


def usage_summary_from_payload(payload):
    usage = (payload or {}).get("usage", {}) or {}
    return {
        "total_requests": int(usage.get("total_requests", 0) or 0),
        "success_count": int(usage.get("success_count", 0) or 0),
        "failure_count": int(usage.get("failure_count", 0) or 0),
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
        "tokens_by_day": dict(usage.get("tokens_by_day", {}) or {}),
        "requests_by_day": dict(usage.get("requests_by_day", {}) or {}),
        "tokens_by_hour": dict(usage.get("tokens_by_hour", {}) or {}),
        "requests_by_hour": dict(usage.get("requests_by_hour", {}) or {}),
        "success_by_hour": dict(usage.get("success_by_hour", {}) or {}),
        "failure_by_hour": dict(usage.get("failure_by_hour", {}) or {}),
        "latency_sum_by_hour": dict(usage.get("latency_sum_by_hour", {}) or {}),
        "latency_count_by_hour": dict(usage.get("latency_count_by_hour", {}) or {}),
        "avg_latency_ms_by_hour": dict(usage.get("avg_latency_ms_by_hour", {}) or {}),
    }


def merge_usage_summary_payloads(results):
    merged = {
        "total_requests": 0,
        "success_count": 0,
        "failure_count": 0,
        "total_tokens": 0,
        "tokens_by_day": defaultdict(int),
        "requests_by_day": defaultdict(int),
        "tokens_by_hour": defaultdict(int),
        "requests_by_hour": defaultdict(int),
        "success_by_hour": defaultdict(int),
        "failure_by_hour": defaultdict(int),
        "latency_sum_by_hour": defaultdict(int),
        "latency_count_by_hour": defaultdict(int),
    }
    errors = []
    node_summaries = []
    for node_name, payload, err in results:
        if err:
            errors.append({"node": node_name, "error": err})
            continue
        summary = usage_summary_from_payload(payload)
        for key in ("total_requests", "success_count", "failure_count", "total_tokens"):
            merged[key] += summary.get(key, 0)
        for key in ("tokens_by_day", "requests_by_day", "tokens_by_hour", "requests_by_hour", "success_by_hour", "failure_by_hour", "latency_sum_by_hour", "latency_count_by_hour"):
            for bucket, value in (summary.get(key, {}) or {}).items():
                merged[key][bucket] += int(value or 0)
        node_summaries.append({"node": node_name, **{k: summary.get(k, 0) for k in ("total_requests", "success_count", "failure_count", "total_tokens")}})
    merged["avg_latency_ms_by_hour"] = {
        hour: round(merged["latency_sum_by_hour"][hour] / count, 2)
        for hour, count in merged["latency_count_by_hour"].items()
        if count
    }
    for key in ("tokens_by_day", "requests_by_day", "tokens_by_hour", "requests_by_hour", "success_by_hour", "failure_by_hour", "latency_sum_by_hour", "latency_count_by_hour", "avg_latency_ms_by_hour"):
        merged[key] = dict(merged[key])
    return {"usage": merged, "failed_requests": merged["failure_count"], "nodes": node_summaries, "node_errors": errors}


def get_cluster_usage_summary():
    def fetch(node):
        payload, err = call_management_api_node(node, "GET", "/v0/management/usage/summary", timeout=10)
        if err:
            payload, err = call_management_api_node(node, "GET", "/v0/management/usage", timeout=30)
        if err:
            print(f"[Cluster] {node['name']} usage summary failed: {err}")
        return node["name"], payload, err

    workers = min(max(len(CLIPROXY_NODES), 1), 8)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        future_by_index = {
            executor.submit(fetch, node): index
            for index, node in enumerate(CLIPROXY_NODES)
        }
        results = [None] * len(CLIPROXY_NODES)
        for future in as_completed(future_by_index):
            index = future_by_index[future]
            node = CLIPROXY_NODES[index]
            try:
                results[index] = future.result()
            except Exception as e:
                results[index] = (node["name"], None, str(e))
                print(f"[Cluster] {node['name']} usage summary failed: {e}")
    return merge_usage_summary_payloads([result for result in results if result is not None])


def _sum_beijing_today(usage, beijing_today_str):
    """Sum tokens/requests for Beijing today from UTC by_day buckets."""
    from datetime import datetime, timedelta
    utc_now = datetime.utcnow()
    beijing_now = utc_now + timedelta(hours=8)
    utc_now_date = utc_now.strftime("%Y-%m-%d")

    tokens_by_day = usage.get("tokens_by_day") or {}
    requests_by_day = usage.get("requests_by_day") or {}

    if utc_now_date == beijing_today_str:
        # UTC day == Beijing day (Beijing 08:00~23:59), use UTC today directly
        tokens = int(tokens_by_day.get(utc_now_date, 0) or 0)
        requests = int(requests_by_day.get(utc_now_date, 0) or 0)
    else:
        # Gap period: Beijing already next day but UTC still previous day
        # (Beijing 00:00~07:59). Estimate by hours elapsed in Beijing today.
        hours_into_beijing_today = beijing_now.hour + beijing_now.minute / 60.0
        # UTC yesterday has 24h of data; Beijing today's portion = last N hours (since 16:00 UTC)
        utc_yesterday_total_tokens = int(tokens_by_day.get(utc_now_date, 0) or 0)
        utc_yesterday_total_requests = int(requests_by_day.get(utc_now_date, 0) or 0)
        # Fraction: hours_into_beijing_today / 24 of yesterday's data
        # (assumes uniform distribution; good enough for dashboard display)
        fraction = min(hours_into_beijing_today / 24.0, 1.0)
        tokens = int(utc_yesterday_total_tokens * fraction)
        requests = int(utc_yesterday_total_requests * fraction)

    return tokens, requests


def token_cost_usd(input_tokens=0, output_tokens=0, cached_tokens=0, reasoning_tokens=0, unknown_tokens=0):
    input_tokens = int(input_tokens or 0)
    cached_tokens = int(cached_tokens or 0)
    unknown_tokens = int(unknown_tokens or 0)
    billable_input_tokens = max(0, input_tokens - cached_tokens) + unknown_tokens
    return (
        billable_input_tokens * TOKEN_PRICING_USD_PER_1M["input"] +
        int(output_tokens or 0) * TOKEN_PRICING_USD_PER_1M["output"] +
        cached_tokens * TOKEN_PRICING_USD_PER_1M["cached"] +
        int(reasoning_tokens or 0) * TOKEN_PRICING_USD_PER_1M["reasoning"]
    ) / 1_000_000


def build_token_breakdown(total_tokens, input_tokens=0, output_tokens=0, cached_tokens=0, reasoning_tokens=0):
    total_tokens = int(total_tokens or 0)
    input_tokens = int(input_tokens or 0)
    output_tokens = int(output_tokens or 0)
    cached_tokens = int(cached_tokens or 0)
    reasoning_tokens = int(reasoning_tokens or 0)
    known_tokens = input_tokens + output_tokens + reasoning_tokens
    unknown_tokens = max(0, total_tokens - known_tokens)
    cost_usd = token_cost_usd(input_tokens, output_tokens, cached_tokens, reasoning_tokens, unknown_tokens)
    return {
        "total_tokens": total_tokens,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "cached_tokens": cached_tokens,
        "reasoning_tokens": reasoning_tokens,
        "unknown_tokens": unknown_tokens,
        "cost_usd": round(cost_usd, 4),
    }


def token_breakdown_totals_from_db(today):
    totals = {
        "today": {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0},
        "total": {"total_tokens": 0, "input_tokens": 0, "output_tokens": 0, "cached_tokens": 0, "reasoning_tokens": 0},
    }
    for row in db.get_daily_usage_history():
        total_tokens = int(row.get("total_tokens", 0) or 0)
        input_tokens = int(row.get("input_tokens", 0) or 0)
        output_tokens = int(row.get("output_tokens", 0) or 0)
        cached_tokens = int(row.get("cached_tokens", 0) or 0)
        reasoning_tokens = int(row.get("reasoning_tokens", 0) or 0)
        totals["total"]["total_tokens"] += total_tokens
        totals["total"]["input_tokens"] += input_tokens
        totals["total"]["output_tokens"] += output_tokens
        totals["total"]["cached_tokens"] += cached_tokens
        totals["total"]["reasoning_tokens"] += reasoning_tokens
        if row.get("date") == today:
            totals["today"]["total_tokens"] = total_tokens
            totals["today"]["input_tokens"] = input_tokens
            totals["today"]["output_tokens"] = output_tokens
            totals["today"]["cached_tokens"] = cached_tokens
            totals["today"]["reasoning_tokens"] = reasoning_tokens

    user_totals = db.get_all_users_total_usage()
    user_total = {
        "total_tokens": sum(int(item.get("total_tokens", 0) or 0) for item in user_totals),
        "input_tokens": sum(int(item.get("input_tokens", 0) or 0) for item in user_totals),
        "output_tokens": sum(int(item.get("output_tokens", 0) or 0) for item in user_totals),
        "cached_tokens": sum(int(item.get("cached_tokens", 0) or 0) for item in user_totals),
        "reasoning_tokens": sum(int(item.get("reasoning_tokens", 0) or 0) for item in user_totals),
    }
    if user_total["total_tokens"] > totals["total"]["total_tokens"]:
        totals["total"] = user_total

    today_user_rows = [item for item in db.get_user_usage_by_period("day") if item.get("period") == today]
    today_user = {
        "total_tokens": sum(int(item.get("total_tokens", 0) or 0) for item in today_user_rows),
        "input_tokens": sum(int(item.get("input_tokens", 0) or 0) for item in today_user_rows),
        "output_tokens": sum(int(item.get("output_tokens", 0) or 0) for item in today_user_rows),
        "cached_tokens": sum(int(item.get("cached_tokens", 0) or 0) for item in today_user_rows),
        "reasoning_tokens": sum(int(item.get("reasoning_tokens", 0) or 0) for item in today_user_rows),
    }
    if today_user["total_tokens"] > totals["today"]["total_tokens"]:
        totals["today"] = today_user
    return totals


def estimate_beijing_today_breakdown_from_utc_day(today_tokens):
    from datetime import datetime, timedelta
    utc_now = datetime.utcnow()
    beijing_now = utc_now + timedelta(hours=8)
    beijing_today_str = beijing_now.strftime("%Y-%m-%d")
    utc_now_date = utc_now.strftime("%Y-%m-%d")
    if utc_now_date == beijing_today_str:
        return None

    utc_day = None
    for row in db.get_daily_usage_history():
        if row.get("date") == utc_now_date:
            utc_day = row
            break
    if not utc_day:
        return None

    utc_total_tokens = int(utc_day.get("total_tokens", 0) or 0)
    today_tokens = int(today_tokens or 0)
    if utc_total_tokens <= 0 or today_tokens <= 0:
        return None

    fraction = min(today_tokens / utc_total_tokens, 1.0)
    return {
        "total_tokens": today_tokens,
        "input_tokens": int(int(utc_day.get("input_tokens", 0) or 0) * fraction),
        "output_tokens": int(int(utc_day.get("output_tokens", 0) or 0) * fraction),
        "cached_tokens": int(int(utc_day.get("cached_tokens", 0) or 0) * fraction),
        "reasoning_tokens": int(int(utc_day.get("reasoning_tokens", 0) or 0) * fraction),
    }


def attach_token_breakdown(summary):
    today = summary.get("today") or beijing_today()
    try:
        totals = token_breakdown_totals_from_db(today)
    except Exception as e:
        print(f"[UsageSummary] Token breakdown unavailable: {e}")
        totals = {
            "today": {"total_tokens": summary.get("today_tokens", 0)},
            "total": {"total_tokens": summary.get("total_tokens", 0)},
        }

    summary_today_tokens = int(summary.get("today_tokens", 0) or 0)
    totals["today"]["total_tokens"] = max(summary_today_tokens, int(totals["today"].get("total_tokens", 0) or 0))
    today_known_tokens = int(totals["today"].get("input_tokens", 0) or 0) + int(totals["today"].get("output_tokens", 0) or 0) + int(totals["today"].get("reasoning_tokens", 0) or 0)
    if summary_today_tokens > 0 and today_known_tokens * 10 < summary_today_tokens:
        estimated_today = estimate_beijing_today_breakdown_from_utc_day(summary_today_tokens)
        if estimated_today:
            totals["today"] = estimated_today

    totals["total"]["total_tokens"] = max(int(summary.get("total_tokens", 0) or 0), int(totals["total"].get("total_tokens", 0) or 0))
    summary["token_breakdown"] = {
        "today": build_token_breakdown(**totals["today"]),
        "total": build_token_breakdown(**totals["total"]),
        "pricing": {
            "usd_per_1m": TOKEN_PRICING_USD_PER_1M,
            "note": "按 GPT-5.5 API 标准价估算：input $5/1M，cached input $0.5/1M，output $30/1M；reasoning token 通常包含在 output 中，不单独计价；未分类 token 暂按 input 价估算。",
        },
    }
    return summary


def build_usage_summary_response(payload):
    usage = usage_summary_from_payload(payload)
    today = beijing_today()
    today_tokens, today_requests = _sum_beijing_today(usage, today)

    total_requests = int(usage.get("total_requests", 0) or 0)
    success_count = int(usage.get("success_count", 0) or 0)
    failure_count = int(usage.get("failure_count", 0) or 0)
    return {
        "today": today,
        "today_tokens": today_tokens,
        "today_requests": today_requests,
        "total_tokens": int(usage.get("total_tokens", 0) or 0),
        "total_requests": total_requests,
        "success_count": success_count,
        "failure_count": failure_count,
        "failed_requests": failure_count,
    }


def clear_persistent_floor_cache():
    with _persistent_floor_cache_lock:
        _persistent_floor_cache["data"] = None
        _persistent_floor_cache["last_update"] = 0


def persistent_usage_summary_floor():
    now = time.time()
    with _persistent_floor_cache_lock:
        cached = _persistent_floor_cache["data"]
        if cached and (now - _persistent_floor_cache["last_update"]) < _persistent_floor_cache["ttl"]:
            return dict(cached)

    try:
        today = beijing_today()
        totals = {
            "today": today,
            "today_tokens": 0,
            "today_requests": 0,
            "total_tokens": 0,
            "total_requests": 0,
            "success_count": 0,
            "failure_count": 0,
        }
        for row in db.get_daily_usage_history():
            tokens = int(row.get("total_tokens", 0) or 0)
            requests_count = int(row.get("total_requests", 0) or 0)
            success_count = int(row.get("success_count", 0) or 0)
            failure_count = int(row.get("failure_count", 0) or 0)
            totals["total_tokens"] += tokens
            totals["total_requests"] += requests_count
            totals["success_count"] += success_count
            totals["failure_count"] += failure_count
            if row.get("date") == today:
                totals["today_tokens"] = tokens
                totals["today_requests"] = requests_count
        user_totals = db.get_all_users_total_usage()
        user_total_tokens = sum(int(item.get("total_tokens", 0) or 0) for item in user_totals)
        user_total_requests = sum(int(item.get("total_requests", 0) or 0) for item in user_totals)
        user_success_count = sum(int(item.get("success_count", 0) or 0) for item in user_totals)
        user_failure_count = sum(int(item.get("failure_count", 0) or 0) for item in user_totals)
        if user_total_tokens > totals["total_tokens"]:
            totals["total_tokens"] = user_total_tokens
            totals["total_requests"] = user_total_requests
            totals["success_count"] = user_success_count
            totals["failure_count"] = user_failure_count

        today_user_rows = [item for item in db.get_user_usage_by_period("day") if item.get("period") == today]
        today_user_tokens = sum(int(item.get("total_tokens", 0) or 0) for item in today_user_rows)
        today_user_requests = sum(int(item.get("total_requests", 0) or 0) for item in today_user_rows)
        totals["today_tokens"] = max(totals["today_tokens"], today_user_tokens)
        totals["today_requests"] = max(totals["today_requests"], today_user_requests)

        totals["failed_requests"] = totals["failure_count"]
        with _persistent_floor_cache_lock:
            _persistent_floor_cache["data"] = dict(totals)
            _persistent_floor_cache["last_update"] = time.time()
        return totals
    except Exception as e:
        print(f"[UsageSummary] Persistent floor unavailable: {e}")
        return None


def _apply_floor_with_live_delta(summary, persistent, key, baseline_key=None):
    live_value = int(summary.get(key, 0) or 0)
    floor_value = int(persistent.get(key, 0) or 0)
    if live_value >= floor_value:
        summary[key] = live_value
        with _usage_floor_delta_baseline_lock:
            _usage_floor_delta_baseline["data"].pop(baseline_key or key, None)
        return

    baseline_key = baseline_key or key
    with _usage_floor_delta_baseline_lock:
        baseline = _usage_floor_delta_baseline["data"].get(baseline_key)
        if not baseline or baseline.get("floor") != floor_value:
            baseline = {"floor": floor_value, "live": live_value}
            _usage_floor_delta_baseline["data"][baseline_key] = baseline
    summary[key] = floor_value + max(0, live_value - int(baseline.get("live", 0) or 0))


def apply_persistent_usage_floor(summary):
    persistent = persistent_usage_summary_floor()
    if not persistent:
        return summary
    for key in ("total_tokens", "total_requests", "success_count", "failure_count", "failed_requests"):
        _apply_floor_with_live_delta(summary, persistent, key)
    if summary.get("today") == persistent.get("today"):
        today = summary.get("today") or ""
        for key in ("today_tokens", "today_requests"):
            _apply_floor_with_live_delta(summary, persistent, key, f"{today}:{key}")
    summary["persistent_floor"] = {
        "total_tokens": persistent.get("total_tokens", 0),
        "total_requests": persistent.get("total_requests", 0),
        "today_tokens": persistent.get("today_tokens", 0),
        "today_requests": persistent.get("today_requests", 0),
    }
    return summary


def get_usage_summary_cached():
    now = time.time()
    with _usage_summary_cache_lock:
        cached = _usage_summary_cache["data"]
        if cached and (now - _usage_summary_cache["last_update"]) < _usage_summary_cache["ttl"]:
            return dict(cached, cache_age_seconds=round(now - _usage_summary_cache["last_update"], 3)), None

    if _stats_cache["data"] and (now - _stats_cache["last_update"]) < _stats_cache["ttl"]:
        summary = build_usage_summary_response(_stats_cache["data"])
    else:
        data = get_cluster_usage_summary()
        summary = build_usage_summary_response(data)
    summary = apply_persistent_usage_floor(summary)
    summary = attach_token_breakdown(summary)

    now = time.time()
    with _usage_summary_cache_lock:
        _usage_summary_cache["data"] = summary
        _usage_summary_cache["last_update"] = now
    return dict(summary, cache_age_seconds=0), None


def get_cluster_auth_files():
    files = []
    errors = []
    for node_name, payload, err in call_management_api_all("GET", "/v0/management/auth-files", timeout=30):
        if err:
            errors.append({"node": node_name, "error": err})
            continue
        for item in (payload or {}).get("files", []) or []:
            if isinstance(item, dict):
                item = dict(item)
                item["node"] = node_name
                files.append(item)
    return files, errors


def parse_detail_time(value):
    if not value:
        return None
    text = str(value)
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(Z|[+-]\d{2}:?\d{2})?$", text)
    if match:
        frac = match.group(2)[:6]
        suffix = match.group(3) or ""
        text = f"{match.group(1)}.{frac}{suffix}"
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def parse_detail_time_utc(value):
    if not value:
        return None
    text = str(value)
    match = re.match(r"^(.*T\d{2}:\d{2}:\d{2})\.(\d+)(Z|[+-]\d{2}:?\d{2})?$", text)
    if match:
        frac = match.group(2)[:6]
        suffix = match.group(3) or ""
        text = f"{match.group(1)}.{frac}{suffix}"
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except Exception:
        return None
    if parsed.tzinfo is None:
        return parsed
    return parsed.astimezone(timezone.utc).replace(tzinfo=None)


def beijing_date_hour(value):
    parsed = parse_detail_time_utc(value)
    if not parsed:
        return None, None
    beijing_time = parsed + timedelta(hours=8)
    return beijing_time.strftime("%Y-%m-%d"), beijing_time.hour


def beijing_today():
    return (datetime.utcnow() + timedelta(hours=8)).strftime("%Y-%m-%d")


def key_usage_for_date(api_stats, date):
    totals = {
        "total_requests": 0,
        "success_count": 0,
        "failure_count": 0,
        "total_tokens": 0,
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_tokens": 0,
        "reasoning_tokens": 0,
    }
    for model_stats in (api_stats.get("models", {}) or {}).values():
        for detail in model_stats.get("details", []) or []:
            if not isinstance(detail, dict):
                continue
            detail_date, _ = beijing_date_hour(str(detail.get("timestamp") or ""))
            if detail_date != date:
                continue
            tokens_info = detail.get("tokens", {}) or {}
            failed_value = detail.get("failed", False)
            failed = failed_value.lower() == "true" if isinstance(failed_value, str) else bool(failed_value)
            totals["total_requests"] += 1
            if failed:
                totals["failure_count"] += 1
            else:
                totals["success_count"] += 1
            totals["total_tokens"] += int(tokens_info.get("total_tokens", 0) or 0)
            totals["input_tokens"] += int(tokens_info.get("input_tokens", 0) or 0)
            totals["output_tokens"] += int(tokens_info.get("output_tokens", 0) or 0)
            totals["cached_tokens"] += int(tokens_info.get("cached_tokens", 0) or 0)
            totals["reasoning_tokens"] += int(tokens_info.get("reasoning_tokens", 0) or 0)
    return totals


def recent_hour_empty_buckets(hours=48):
    now_beijing = datetime.utcnow() + timedelta(hours=8)
    end_hour = now_beijing.replace(minute=0, second=0, microsecond=0)
    start_hour = end_hour - timedelta(hours=hours - 1)
    buckets = {}
    for index in range(hours):
        hour_value = start_hour + timedelta(hours=index)
        key = hour_value.strftime("%Y-%m-%d %H")
        buckets[key] = {
            "key": key,
            "label": hour_value.strftime("%m-%d %H:00"),
            "tokens": 0,
            "requests": 0,
            "success_count": 0,
            "failure_count": 0,
            "latency_sum_ms": 0,
            "latency_count": 0,
            "avg_latency_ms": None,
        }
    return buckets


def finalize_recent_hour_buckets(buckets):
    recent_hours = []
    for bucket in buckets.values():
        if bucket["latency_count"]:
            bucket["avg_latency_ms"] = round(bucket["latency_sum_ms"] / bucket["latency_count"], 2)
        bucket.pop("latency_sum_ms", None)
        bucket.pop("latency_count", None)
        recent_hours.append(bucket)
    return recent_hours


def distribute_total_by_weight(items, total, field, weight_field):
    total = int(total or 0)
    weights = [max(0, int(item.get(weight_field, 0) or 0)) for item in items]
    weight_sum = sum(weights)
    if total <= 0:
        for item in items:
            item[field] = 0
        return
    if weight_sum <= 0:
        base, rem = divmod(total, len(items))
        for index, item in enumerate(items):
            item[field] = base + (1 if index < rem else 0)
        return
    assigned = []
    running = 0
    for index, item in enumerate(items):
        raw = total * weights[index] / weight_sum
        value = int(raw)
        assigned.append((raw - value, index, value))
        running += value
    remainder = total - running
    assigned.sort(reverse=True)
    values = [value for _, _, value in assigned]
    for offset in range(remainder):
        fraction, index, value = assigned[offset]
        assigned[offset] = (fraction, index, value + 1)
    for _, index, value in assigned:
        items[index][field] = value


def normalize_recent_days(buckets, usage):
    today = beijing_today()
    daily_rows = {row.get("date"): row for row in db.get_daily_usage_history()}
    by_day = defaultdict(list)
    for bucket in buckets.values():
        by_day[bucket["key"][:10]].append(bucket)
    for date, items in by_day.items():
        daily = daily_rows.get(date)
        if date == today:
            tokens_by_day = usage.get("tokens_by_day", {}) or {}
            requests_by_day = usage.get("requests_by_day", {}) or {}
            daily = dict(daily or {})
            daily["total_tokens"] = max(
                int(daily.get("total_tokens", 0) or 0),
                int(tokens_by_day.get(today, 0) or 0),
            )
            daily["total_requests"] = max(
                int(daily.get("total_requests", 0) or 0),
                int(requests_by_day.get(today, 0) or 0),
            )
        elif len(items) != 24:
            continue
        if not daily:
            continue
        total_requests = int(daily.get("total_requests", 0) or 0)
        failure_total = int(daily.get("failure_count", 0) or 0)
        if date == today and total_requests:
            stored_requests = int((daily_rows.get(date) or {}).get("total_requests", 0) or 0)
            if stored_requests and stored_requests != total_requests:
                failure_total = round(failure_total * total_requests / stored_requests)
        distribute_total_by_weight(items, daily.get("total_tokens", 0), "tokens", "tokens")
        distribute_total_by_weight(items, total_requests, "requests", "requests")
        distribute_total_by_weight(items, failure_total, "failure_count", "failure_count")
        for item in items:
            if item["failure_count"] > item["requests"]:
                item["failure_count"] = item["requests"]
            item["success_count"] = item["requests"] - item["failure_count"]


def recent_hour_fallback_from_summary(usage, hours=48):
    buckets = recent_hour_empty_buckets(hours)
    start_key = next(iter(buckets))
    end_key = next(reversed(buckets))
    for key, row in db.get_hourly_usage_range(start_key, end_key).items():
        bucket = buckets.get(key)
        if not bucket:
            continue
        bucket["tokens"] = int(row.get("tokens", 0) or 0)
        bucket["requests"] = int(row.get("requests", 0) or 0)
        bucket["success_count"] = int(row.get("success_count", 0) or 0)
        bucket["failure_count"] = int(row.get("failure_count", 0) or 0)
        latency_count = int(row.get("latency_count", 0) or 0)
        if latency_count:
            bucket["avg_latency_ms"] = round(int(row.get("latency_sum_ms", 0) or 0) / latency_count, 2)

    tokens_by_hour = usage.get("tokens_by_hour", {}) or {}
    requests_by_hour = usage.get("requests_by_hour", {}) or {}
    success_by_hour = usage.get("success_by_hour", {}) or {}
    failure_by_hour = usage.get("failure_by_hour", {}) or {}
    latency_sum_by_hour = usage.get("latency_sum_by_hour", {}) or {}
    latency_count_by_hour = usage.get("latency_count_by_hour", {}) or {}
    avg_latency_ms_by_hour = usage.get("avg_latency_ms_by_hour", {}) or {}
    today = beijing_today()
    for bucket in buckets.values():
        if bucket["key"][:10] != today:
            continue
        utc_hour = (int(bucket["key"][-2:]) - 8) % 24
        hour_key = f"{utc_hour:02d}"
        tokens = int(tokens_by_hour.get(hour_key, 0) or 0)
        requests = int(requests_by_hour.get(hour_key, 0) or 0)
        success_count = int(success_by_hour.get(hour_key, 0) or 0)
        failure_count = int(failure_by_hour.get(hour_key, 0) or 0)
        latency_sum = int(latency_sum_by_hour.get(hour_key, 0) or 0)
        latency_count = int(latency_count_by_hour.get(hour_key, 0) or 0)
        if not (tokens or requests or success_count or failure_count or latency_count):
            continue
        bucket["tokens"] = tokens
        bucket["requests"] = requests
        bucket["success_count"] = success_count
        bucket["failure_count"] = failure_count
        bucket["avg_latency_ms"] = avg_latency_ms_by_hour.get(hour_key)
        db.upsert_hourly_usage(bucket["key"], tokens, requests, success_count, failure_count, latency_sum, latency_count)
    normalize_recent_days(buckets, usage)
    return finalize_recent_hour_buckets(buckets)


def build_recent_hour_usage(payload, hours=48):
    buckets = recent_hour_empty_buckets(hours)
    start_hour = datetime.strptime(next(iter(buckets)), "%Y-%m-%d %H")
    end_hour = datetime.strptime(next(reversed(buckets)), "%Y-%m-%d %H")
    start_utc_key = (start_hour - timedelta(hours=8)).strftime("%Y-%m-%dT%H")
    end_utc_key = (end_hour - timedelta(hours=8)).strftime("%Y-%m-%dT%H")

    for api_stats in ((payload or {}).get("usage", {}).get("apis", {}) or {}).values():
        for model_stats in (api_stats.get("models", {}) or {}).values():
            for detail in model_stats.get("details", []) or []:
                if not isinstance(detail, dict):
                    continue
                timestamp = str(detail.get("timestamp") or "")
                timestamp_hour = timestamp[:13]
                if timestamp_hour < start_utc_key or timestamp_hour > end_utc_key:
                    continue
                parsed = parse_detail_time_utc(timestamp)
                if not parsed:
                    continue
                beijing_time = parsed + timedelta(hours=8)
                hour_value = beijing_time.replace(minute=0, second=0, microsecond=0)
                if hour_value < start_hour or hour_value > end_hour:
                    continue
                bucket = buckets.get(hour_value.strftime("%Y-%m-%d %H"))
                if not bucket:
                    continue
                tokens_info = detail.get("tokens", {}) or {}
                failed_value = detail.get("failed", False)
                failed = failed_value.lower() == "true" if isinstance(failed_value, str) else bool(failed_value)
                bucket["requests"] += 1
                if failed:
                    bucket["failure_count"] += 1
                else:
                    bucket["success_count"] += 1
                bucket["tokens"] += int(tokens_info.get("total_tokens", 0) or 0)
                try:
                    latency_ms = int(detail.get("latency_ms") or 0)
                except Exception:
                    latency_ms = 0
                if latency_ms > 0:
                    bucket["latency_sum_ms"] += latency_ms
                    bucket["latency_count"] += 1

    return finalize_recent_hour_buckets(buckets)


def refresh_recent_hours_cache():
    try:
        full_usage = get_cluster_usage()
        if not full_usage:
            return
        recent_hours = build_recent_hour_usage(full_usage, hours=48)
        with _recent_hours_cache_lock:
            _recent_hours_cache["data"] = recent_hours
            _recent_hours_cache["last_update"] = time.time()
    finally:
        with _recent_hours_cache_lock:
            _recent_hours_cache["refreshing"] = False


def start_recent_hours_refresh():
    with _recent_hours_cache_lock:
        if _recent_hours_cache["refreshing"]:
            return False
        _recent_hours_cache["refreshing"] = True
    threading.Thread(target=refresh_recent_hours_cache, daemon=True).start()
    return True


def get_recent_hours_cached(summary_usage):
    now = time.time()
    with _recent_hours_cache_lock:
        cached = _recent_hours_cache["data"]
        last_update = _recent_hours_cache["last_update"]
        ttl = _recent_hours_cache["ttl"]
    if cached and (now - last_update) < ttl:
        return cached, False

    recent_hours = recent_hour_fallback_from_summary(summary_usage or {}, 48)
    with _recent_hours_cache_lock:
        _recent_hours_cache["data"] = recent_hours
        _recent_hours_cache["last_update"] = now
        _recent_hours_cache["refreshing"] = False
    return recent_hours, False


def build_auth_stats():
    try:
        usage_payload = get_cluster_usage()
    except Exception:
        usage_payload = None
    if not usage_payload:
        usage_payload = {"usage": {}, "node_errors": [{"node": "cluster", "error": "usage unavailable"}]}
    files, auth_errors = get_cluster_auth_files()
    now = datetime.utcnow()
    today = datetime.now().strftime("%Y-%m-%d")
    node_by_name = {node["name"]: node for node in CLIPROXY_NODES}
    windows = {
        "last_1h": now - timedelta(hours=1),
        "last_5h": now - timedelta(hours=5),
        "last_24h": now - timedelta(hours=24),
        "last_7d": now - timedelta(days=2),
        "total": None,
    }
    window_names = tuple(windows.keys())

    stats = {}
    quota_sources = {}

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
        breakdown = build_token_breakdown(
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
    account_map = {}
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
            "_quota_usage_details": [],
            "quota": {
                "status": "unsupported",
                "provider": f.get("provider") or f.get("type", ""),
                "windows": {},
            },
        }
        for window_name in window_names:
            stats[stats_key][window_name] = empty_window()
        if stats[stats_key].get("auth_index"):
            auth_index_map[stats[stats_key]["auth_index"]] = stats[stats_key]
        if account:
            account_map[account] = stats[stats_key]
        provider = str(stats[stats_key]["provider"] or "").strip().lower()
        if provider in ("codex", "claude"):
            node_cfg = node_by_name.get(node)
            if node_cfg:
                quota_sources[stats_key] = (node_cfg, f)

    def find_stat(detail):
        auth_index = detail.get("auth_index")
        source = detail.get("source")
        if auth_index and auth_index in auth_index_map:
            return auth_index_map[auth_index]
        if source and source in account_map:
            return account_map[source]
        for account, stat in account_map.items():
            if source and source in account:
                return stat
        return None

    for api_stats in (usage_payload.get("usage", {}).get("apis", {}) or {}).values():
        for model_stats in (api_stats.get("models", {}) or {}).values():
            for detail in model_stats.get("details", []) or []:
                if not isinstance(detail, dict):
                    continue
                stat = find_stat(detail)
                if not stat:
                    continue
                when = parse_detail_time(detail.get("timestamp"))
                when_utc = parse_detail_time_utc(detail.get("timestamp"))
                tokens_info = detail.get("tokens") or {}
                tokens = int(tokens_info.get("total_tokens", 0) or 0)
                input_tokens = int(tokens_info.get("input_tokens", 0) or 0)
                output_tokens = int(tokens_info.get("output_tokens", 0) or 0)
                cached_tokens = int(tokens_info.get("cached_tokens", 0) or 0)
                reasoning_tokens = int(tokens_info.get("reasoning_tokens", 0) or 0)
                failed = bool(detail.get("failed", False))
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
                if when and (not stat["last_request_at"] or when > parse_detail_time(stat["last_request_at"])):
                    stat["last_request_at"] = detail.get("timestamp", "")
                if failed and when and (not stat["last_error_at"] or when > parse_detail_time(stat["last_error_at"])):
                    stat["last_error_at"] = detail.get("timestamp", "")
                    stat["last_error_message"] = detail_error_message(detail)
                    stat["last_error_status"] = detail_error_status(detail)

    node_summary = {}
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

    for summary in node_summary.values():
        for window_name in window_names:
            enrich_window(summary[window_name])

    if quota_sources:
        workers = min(6, len(quota_sources))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            future_map = {
                executor.submit(get_auth_quota_cached, node_cfg, auth_file): stats_key
                for stats_key, (node_cfg, auth_file) in quota_sources.items()
                if stats_key in stats
            }
            for future in as_completed(future_map):
                stats_key = future_map[future]
                try:
                    stats[stats_key]["quota"] = future.result()
                except Exception as e:
                    stats[stats_key]["quota"] = _build_error_quota(stats[stats_key].get("provider", ""), str(e))

    apply_quota_window_usage(stats)
    today_used_tokens = int(((usage_payload.get("usage") or {}).get("tokens_by_day") or {}).get(today, 0) or 0)
    today_quota_usage = build_today_quota_usage(stats, today, today_used_tokens)
    for stat in stats.values():
        stat.pop("_quota_usage_details", None)

    return {
        "auth_files": sorted(stats.values(), key=lambda x: (x["node"], x["account"])),
        "nodes": node_summary,
        "today_quota_usage": today_quota_usage,
        "errors": usage_payload.get("node_errors", []) + auth_errors,
        "generated_at": datetime.utcnow().isoformat() + "Z",
    }

def get_auth_files():
    """Get list of auth files from all CLIProxyAPI nodes."""
    files, _ = get_cluster_auth_files()
    return files


def get_auth_file_detail(path):
    """Get auth file detail including expiry time."""
    data, err = call_management_api("GET", f"/v0/management/auth-files/download?path={path}")
    if err:
        return None
    return data


def parse_callback_url(raw_input):
    """Parse OAuth callback URL or query string to extract code and state.

    Accepts:
    - Full URL: http://localhost:54545/callback?code=xxx&state=yyy
    - URL without scheme: localhost:54545/callback?code=xxx&state=yyy
    - Just query string: code=xxx&state=yyy
    - Query string with ?: ?code=xxx&state=yyy
    """
    if not raw_input:
        return None, None

    raw_input = raw_input.strip()

    try:
        # Try to extract code and state using regex for robustness
        code_match = re.search(r'[?&]code=([^&\s]+)', raw_input)
        state_match = re.search(r'[?&]state=([^&\s]+)', raw_input)

        # Also try without leading ? or &
        if not code_match:
            code_match = re.search(r'^code=([^&\s]+)', raw_input)
        if not state_match:
            state_match = re.search(r'^state=([^&\s]+)', raw_input) or re.search(r'&state=([^&\s]+)', raw_input)

        code = code_match.group(1) if code_match else None
        state = state_match.group(1) if state_match else None

        # URL decode if needed
        if code:
            from urllib.parse import unquote
            code = unquote(code)
        if state:
            from urllib.parse import unquote
            state = unquote(state)

        return code, state
    except Exception:
        return None, None


def validate_oauth_params(code, state):
    """Validate OAuth parameters."""
    errors = []

    if not code:
        errors.append("缺少 code 参数")
    elif len(code) < 10:
        errors.append("code 参数格式不正确")

    if not state:
        errors.append("缺少 state 参数")
    elif len(state) != 32 or not re.match(r'^[a-f0-9]+$', state):
        errors.append("state 参数格式不正确")

    return errors


# Usage History - Memory Cache + CSV Persistence
USAGE_HISTORY_FILE = os.path.join(os.path.dirname(__file__), "data", "usage_history.csv")
USAGE_CSV_FIELDS = ["date", "total_requests", "success_count", "failure_count", "total_tokens", "input_tokens", "output_tokens"]

# Memory cache for usage history
_usage_history_cache = {
    "data": {},  # date -> {total_requests, success_count, failure_count, total_tokens, input_tokens, output_tokens}
    "loaded": False
}


def load_usage_history():
    """Load usage history from CSV into memory cache."""
    if _usage_history_cache["loaded"]:
        return _usage_history_cache["data"]

    _usage_history_cache["data"] = {}

    if os.path.exists(USAGE_HISTORY_FILE):
        try:
            with open(USAGE_HISTORY_FILE, "r", newline="") as f:
                reader = csv.DictReader(f)
                for row in reader:
                    date = row.get("date", "")
                    if date:
                        _usage_history_cache["data"][date] = {
                            "total_requests": int(row.get("total_requests", 0)),
                            "success_count": int(row.get("success_count", 0)),
                            "failure_count": int(row.get("failure_count", 0)),
                            "total_tokens": int(row.get("total_tokens", 0)),
                            "input_tokens": int(row.get("input_tokens", 0)),
                            "output_tokens": int(row.get("output_tokens", 0)),
                        }
            print(f"[UsageHistory] Loaded {len(_usage_history_cache['data'])} days from CSV")
        except Exception as e:
            print(f"[UsageHistory] Error loading CSV: {e}")

    _usage_history_cache["loaded"] = True
    return _usage_history_cache["data"]


def save_usage_history():
    """Save memory cache to CSV file."""
    try:
        os.makedirs(os.path.dirname(USAGE_HISTORY_FILE), exist_ok=True)

        # Sort by date
        sorted_dates = sorted(_usage_history_cache["data"].keys())

        with open(USAGE_HISTORY_FILE, "w", newline="") as f:
            writer = csv.DictWriter(f, fieldnames=USAGE_CSV_FIELDS)
            writer.writeheader()
            for date in sorted_dates:
                row = {"date": date, **_usage_history_cache["data"][date]}
                writer.writerow(row)

        print(f"[UsageHistory] Saved {len(sorted_dates)} days to CSV")
        return True
    except Exception as e:
        print(f"[UsageHistory] Error saving CSV: {e}")
        return False


def sync_usage_from_api():
    """Sync usage data from CLIProxyAPI to database and CSV."""
    try:
        # 1. Fetch data from all Management APIs
        data = get_cluster_usage()

        # 2. Build key-to-user mapping
        user_keys_data = load_user_keys()
        key_to_user = usage_sync.build_key_to_user_mapping(user_keys_data)

        # 3. Sync to database
        success, stats = usage_sync.sync_usage_to_database(data, key_to_user)
        if not success:
            print(f"[UsageSync] Database sync failed: {stats.get('error', 'unknown error')}")
            return False

        # 4. Also update CSV cache for backward compatibility
        usage = data.get("usage", {})
        tokens_by_day = usage.get("tokens_by_day", {})
        requests_by_day = usage.get("requests_by_day", {})

        load_usage_history()
        for date in set(list(tokens_by_day.keys()) + list(requests_by_day.keys())):
            _usage_history_cache["data"][date] = {
                "total_requests": requests_by_day.get(date, 0),
                "success_count": requests_by_day.get(date, 0),
                "failure_count": 0,
                "total_tokens": tokens_by_day.get(date, 0),
                "input_tokens": 0,
                "output_tokens": 0,
            }

        save_usage_history()
        clear_persistent_floor_cache()

        print(f"[UsageSync] Synced successfully: {stats.get('user_records', 0)} user records, "
              f"{stats.get('daily_records', 0)} days, "
              f"{stats.get('total_tokens', 0):,} tokens")

        return True
    except Exception as e:
        print(f"[UsageSync] Sync error: {e}")
        import traceback
        traceback.print_exc()
        return False


def git_sync_usage_csv():
    """Commit and push usage CSV to GitHub."""
    try:
        repo_dir = os.path.dirname(os.path.dirname(__file__))  # CLIProxyAPI root
        csv_path = "key-portal/data/usage_history.csv"

        # Check if there are changes
        result = subprocess.run(
            ["git", "diff", "--quiet", csv_path],
            cwd=repo_dir,
            capture_output=True
        )

        if result.returncode == 0:
            print("[GitSync] No changes to commit")
            return True

        # Add, commit, push
        subprocess.run(["git", "add", csv_path], cwd=repo_dir, check=True)

        today = datetime.now().strftime("%Y-%m-%d")
        subprocess.run(
            ["git", "commit", "-m", f"Update usage history {today}"],
            cwd=repo_dir,
            check=True
        )

        subprocess.run(["git", "push"], cwd=repo_dir, check=True)

        print(f"[GitSync] Pushed usage history update for {today}")
        return True
    except subprocess.CalledProcessError as e:
        print(f"[GitSync] Git command failed: {e}")
        return False
    except Exception as e:
        print(f"[GitSync] Error: {e}")
        return False


def get_usage_history_aggregated():
    """Get usage history with daily, monthly, and yearly aggregations."""
    # Use database instead of CSV cache
    return db.get_usage_aggregated()


def enrich_usage_breakdowns(data):
    for row in data.get("history", []) or []:
        breakdown = build_token_breakdown(row.get("total_tokens", 0), row.get("input_tokens", 0), row.get("output_tokens", 0), row.get("cached_tokens", 0), row.get("reasoning_tokens", 0))
        row["token_breakdown"] = breakdown
        row["estimated_cost_usd"] = breakdown["cost_usd"]
    for bucket_name in ("by_month", "by_year"):
        for bucket in (data.get(bucket_name, {}) or {}).values():
            breakdown = build_token_breakdown(bucket.get("total_tokens", 0), bucket.get("input_tokens", 0), bucket.get("output_tokens", 0), bucket.get("cached_tokens", 0), bucket.get("reasoning_tokens", 0))
            bucket["token_breakdown"] = breakdown
            bucket["estimated_cost_usd"] = breakdown["cost_usd"]
    data["token_pricing"] = {
        "usd_per_1m": TOKEN_PRICING_USD_PER_1M,
        "note": "按 GPT-5.5 API 标准价估算：input $5/1M，cached input $0.5/1M，output $30/1M；reasoning token 通常包含在 output 中，不单独计价。",
    }
    return data


def apply_live_today_usage(data, usage):
    today = beijing_today()
    tokens_by_day = usage.get("tokens_by_day", {}) or {}
    requests_by_day = usage.get("requests_by_day", {}) or {}

    if today not in tokens_by_day and today not in requests_by_day:
        return data

    live_tokens = int(tokens_by_day.get(today, 0) or 0)
    live_requests = int(requests_by_day.get(today, 0) or 0)
    history = data.setdefault("history", [])
    row = next((item for item in history if item.get("date") == today), None)
    if row is None:
        row = {
            "date": today,
            "total_requests": 0,
            "success_count": 0,
            "failure_count": 0,
            "total_tokens": 0,
            "input_tokens": 0,
            "output_tokens": 0,
        }
        history.append(row)
        history.sort(key=lambda item: item.get("date", ""))

    old_tokens = int(row.get("total_tokens", 0) or 0)
    old_requests = int(row.get("total_requests", 0) or 0)
    old_success = int(row.get("success_count", 0) or 0)
    old_failure = int(row.get("failure_count", 0) or 0)

    row["total_tokens"] = max(old_tokens, live_tokens)
    row["total_requests"] = max(old_requests, live_requests)

    success_by_hour = usage.get("success_by_hour", {}) or {}
    failure_by_hour = usage.get("failure_by_hour", {}) or {}
    live_success = sum(int(value or 0) for value in success_by_hour.values())
    live_failure = sum(int(value or 0) for value in failure_by_hour.values())
    if live_success + live_failure == live_requests:
        row["success_count"] = max(old_success, live_success)
        row["failure_count"] = max(old_failure, live_failure)

    token_delta = int(row.get("total_tokens", 0) or 0) - old_tokens
    request_delta = int(row.get("total_requests", 0) or 0) - old_requests
    success_delta = int(row.get("success_count", 0) or 0) - old_success
    failure_delta = int(row.get("failure_count", 0) or 0) - old_failure

    for bucket_name, bucket_key in (("by_month", today[:7]), ("by_year", today[:4])):
        bucket = data.setdefault(bucket_name, {}).setdefault(bucket_key, {
            "total_tokens": 0,
            "total_requests": 0,
            "success_count": 0,
            "failure_count": 0,
        })
        bucket["total_tokens"] = int(bucket.get("total_tokens", 0) or 0) + token_delta
        bucket["total_requests"] = int(bucket.get("total_requests", 0) or 0) + request_delta
        bucket["success_count"] = int(bucket.get("success_count", 0) or 0) + success_delta
        bucket["failure_count"] = int(bucket.get("failure_count", 0) or 0) + failure_delta

    return data


# ============================================================================
# Snapshot Management (delegated to snapshot module)
# ============================================================================

def snapshot_file_for_node(node_name):
    """Return the per-node snapshot path."""
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", node_name or "unknown")
    return os.path.join(SNAPSHOT_DIR, f"{safe_name}.json")


def snapshot_meta_file_for_node(node_name):
    return f"{snapshot_file_for_node(node_name)}.meta.json"


def snapshot_totals(snapshot_data):
    usage = (snapshot_data or {}).get("usage", {}) or {}
    return int(usage.get("total_tokens", 0) or 0), int(usage.get("total_requests", 0) or 0)


def load_snapshot_file(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        print(f"[Snapshot] Ignoring invalid snapshot {path}: {e}")
        return None


def load_snapshot_meta(path):
    if not path or not os.path.exists(path):
        return None
    try:
        with open(path, "r") as f:
            data = json.load(f)
        return int(data.get("total_tokens", 0) or 0), int(data.get("total_requests", 0) or 0)
    except (json.JSONDecodeError, OSError, ValueError) as e:
        print(f"[Snapshot] Ignoring invalid snapshot metadata {path}: {e}")
        return None


def write_snapshot_meta(path, total_tokens, total_requests):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump({"total_tokens": int(total_tokens or 0), "total_requests": int(total_requests or 0)}, f)
    os.replace(tmp_path, path)


def write_snapshot_file(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp_path, path)


def import_snapshot_to_node(node, snapshot_data, label):
    """Import a snapshot into one node and return whether it succeeded."""
    if not snapshot_data:
        return False
    print(f"[Snapshot] Importing {label} into {node['name']}...")
    data, err = call_management_api_node(node, "POST", "/v0/management/usage/import", snapshot_data, timeout=60)
    if err:
        print(f"[Snapshot] Import failed for {node['name']}: {err}")
        return False
    print(
        f"[Snapshot] Imported {node['name']}: "
        f"added={data.get('added', 0):,}, skipped={data.get('skipped', 0):,}, "
        f"total_requests={data.get('total_requests', 0):,}"
    )
    return True


def export_node_snapshot(node):
    """Export one node snapshot, restoring the last snapshot first if a restart is detected."""
    node_name = node["name"]
    path = snapshot_file_for_node(node_name)
    data, err = call_management_api_node(node, "GET", "/v0/management/usage/export", timeout=60)
    if err:
        print(f"[Snapshot] Export failed for {node_name}: {err}")
        return False

    current_tokens, current_requests = snapshot_totals(data)
    meta_path = snapshot_meta_file_for_node(node_name)
    previous_totals = load_snapshot_meta(meta_path)
    if previous_totals:
        previous_tokens, previous_requests = previous_totals
        if current_tokens < previous_tokens or current_requests < previous_requests:
            print(
                f"[Snapshot] Restart detected on {node_name}: "
                f"current={current_tokens:,}/{current_requests:,}, "
                f"snapshot={previous_tokens:,}/{previous_requests:,}. Restoring before export."
            )
            previous = load_snapshot_file(path)
            if previous and import_snapshot_to_node(node, previous, path):
                data, err = call_management_api_node(node, "GET", "/v0/management/usage/export", timeout=60)
                if err:
                    print(f"[Snapshot] Export after restore failed for {node_name}: {err}")
                    return False
                current_tokens, current_requests = snapshot_totals(data)
            else:
                return False

    write_snapshot_file(path, data)
    write_snapshot_meta(meta_path, current_tokens, current_requests)

    # Keep the legacy single-node file for compatibility with older tooling.
    if node_name == CLIPROXY_NODES[0]["name"]:
        write_snapshot_file(SNAPSHOT_FILE, data)
        write_snapshot_meta(f"{SNAPSHOT_FILE}.meta.json", current_tokens, current_requests)

    print(f"[Snapshot] Exported {node_name}: {current_tokens:,} tokens, {current_requests:,} requests -> {path}")
    return True


def export_cliproxy_snapshot():
    """Export complete usage snapshots from all CLIProxyAPI nodes."""
    try:
        print("[Snapshot] Exporting usage data from CLIProxyAPI nodes...")
        results = [export_node_snapshot(node) for node in CLIPROXY_NODES]
        return any(results)

    except Exception as e:
        print(f"[Snapshot] Export error: {e}")
        import traceback
        traceback.print_exc()
        return False


def import_cliproxy_snapshot():
    """Import previously exported snapshots into CLIProxyAPI nodes."""
    try:
        imported = False
        for index, node in enumerate(CLIPROXY_NODES):
            path = snapshot_file_for_node(node["name"])
            if not os.path.exists(path) and index == 0 and os.path.exists(SNAPSHOT_FILE):
                path = SNAPSHOT_FILE
            snapshot_data = load_snapshot_file(path)
            if not snapshot_data:
                print(f"[Snapshot] No snapshot file found for {node['name']} at {path}")
                continue
            tokens, requests_count = snapshot_totals(snapshot_data)
            print(f"[Snapshot] Found {node['name']} snapshot: {tokens:,} tokens, {requests_count:,} requests")
            imported = import_snapshot_to_node(node, snapshot_data, path) or imported
        return imported

    except Exception as e:
        print(f"[Snapshot] Import error: {e}")
        import traceback
        traceback.print_exc()
        return False


def detect_cliproxy_restart(current_tokens, current_requests):
    """
    Detect if CLIProxyAPI has restarted by checking if token count decreased.
    In-memory statistics only increase, so any decrease indicates a restart.
    """
    from datetime import datetime

    now = datetime.now()

    # First check, initialize state
    if _cliproxy_state["last_check_time"] is None:
        _cliproxy_state["last_total_tokens"] = current_tokens
        _cliproxy_state["last_total_requests"] = current_requests
        _cliproxy_state["last_check_time"] = now
        print(f"[Restart] Monitoring initialized: {current_tokens:,} tokens, {current_requests:,} requests")
        return False

    # Check if data decreased (restart indicator)
    tokens_decreased = current_tokens < _cliproxy_state["last_total_tokens"]
    requests_decreased = current_requests < _cliproxy_state["last_total_requests"]

    if tokens_decreased or requests_decreased:
        _cliproxy_state["restart_count"] += 1

        print()
        print("=" * 80)
        print(f"🔄 CLIProxyAPI RESTART DETECTED! (Restart #{_cliproxy_state['restart_count']})")
        print("=" * 80)
        print(f"Previous state (before restart):")
        print(f"  Tokens:   {_cliproxy_state['last_total_tokens']:,}")
        print(f"  Requests: {_cliproxy_state['last_total_requests']:,}")
        print(f"  Time:     {_cliproxy_state['last_check_time']}")
        print()
        print(f"Current state (after restart):")
        print(f"  Tokens:   {current_tokens:,}")
        print(f"  Requests: {current_requests:,}")
        print(f"  Time:     {now}")
        print()
        print(f"Data loss (in-memory):")
        print(f"  Tokens:   {_cliproxy_state['last_total_tokens'] - current_tokens:,}")
        print(f"  Requests: {_cliproxy_state['last_total_requests'] - current_requests:,}")
        print("=" * 80)
        print()

        # Update state
        _cliproxy_state["last_total_tokens"] = current_tokens
        _cliproxy_state["last_total_requests"] = current_requests
        _cliproxy_state["last_check_time"] = now

        return True

    # Normal growth, update state
    _cliproxy_state["last_total_tokens"] = current_tokens
    _cliproxy_state["last_total_requests"] = current_requests
    _cliproxy_state["last_check_time"] = now

    return False


def scheduled_snapshot_export():
    """Scheduled task to export CLIProxyAPI snapshot."""
    with app.app_context():
        print(f"[Scheduler] Running snapshot export at {datetime.now().isoformat()}")
        export_cliproxy_snapshot()


# ============================================================================
# Routes
# ============================================================================

@app.route("/")
def index():
    """Tutorial page showing how to use the service."""
    service_info = dict(config.SERVICE_INFO)
    service_info["base_url"] = _api_base_url()
    return render_template("index.html", service_info=service_info)


@app.route("/register")
def register_page():
    """User registration page."""
    return render_template("register.html")


@app.route("/my-keys")
def my_keys_page():
    """User's keys management page."""
    service_info = dict(config.SERVICE_INFO)
    service_info["base_url"] = _api_base_url()
    return render_template("my_keys.html", service_info=service_info)


@app.route("/admin/users")
def admin_users_page():
    """Admin page for user statistics."""
    return render_template("admin_users.html")


@app.route("/admin/auth-stats")
def admin_auth_stats_page():
    """Admin page for auth file statistics."""
    return render_template("admin_auth_stats.html")


@app.route("/login")
def login():
    """OAuth login page for contributing keys."""
    return render_template("login.html")


@app.route("/status")
def status():
    """Key status page showing all registered keys."""
    return render_template("status.html")


@app.route("/api/auth-url")
def get_auth_url():
    """Get Claude OAuth authorization URL."""
    data, err = call_management_api("GET", "/v0/management/anthropic-auth-url")
    if err:
        return jsonify({"error": err}), 500
    return jsonify(data)




@app.route("/callback")
def oauth_callback():
    """Handle OAuth callback from Claude - automatically complete authorization."""
    code = request.args.get("code")
    state = request.args.get("state")

    if not code or not state:
        return render_template("callback_result.html", success=False, error="Missing code or state parameter")

    # Call CLIProxyAPI to complete OAuth
    data, err = call_management_api("POST", "/v0/management/oauth-callback", {
        "provider": "anthropic",
        "code": code,
        "state": state
    })

    if err:
        return render_template("callback_result.html", success=False, error=err)

    account = data.get("account", "Unknown")
    return render_template("callback_result.html", success=True, account=account)


@app.route("/api/submit-callback", methods=["POST"])
def submit_callback():
    """Submit OAuth callback URL to complete authorization."""
    body = request.get_json()
    callback_url = body.get("callback_url", "")

    if not callback_url:
        return jsonify({"error": "请粘贴回调链接"}), 400

    code, state = parse_callback_url(callback_url)

    # Validate parameters
    errors = validate_oauth_params(code, state)
    if errors:
        return jsonify({"error": "；".join(errors)}), 400

    # Call CLIProxyAPI to complete OAuth
    data, err = call_management_api("POST", "/v0/management/oauth-callback", {
        "provider": "anthropic",
        "code": code,
        "state": state
    })

    if err:
        # Make error message more user friendly
        if "expired" in err.lower() or "unknown" in err.lower():
            return jsonify({"error": "授权已过期，请重新点击「打开 Claude 授权」"}), 400
        if "not pending" in err.lower():
            return jsonify({"error": "该授权已完成或已失效，请重新授权"}), 400
        return jsonify({"error": err}), 500

    return jsonify({
        "message": "授权成功！Key 将在几秒内生效。",
        "status": "ok"
    })


@app.route("/api/usage")
def get_usage():
    """Get aggregated usage statistics from all CLIProxyAPI nodes."""
    data, err = get_usage_stats_cached()
    if err:
        return jsonify({"error": err, "usage": {}}), 200
    return jsonify(strip_usage_details(data))


@app.route("/api/usage-summary")
def get_usage_summary():
    """Get lightweight dashboard usage counters."""
    data, err = get_usage_summary_cached()
    if err:
        return jsonify({"error": err}), 200
    return jsonify(data)


@app.route("/api/auth-stats")
def get_auth_stats():
    """Get per-auth-file usage windows across all nodes."""
    return jsonify(get_auth_stats_cached())


@app.route("/api/auth-stats/toggle-auth", methods=["POST"])
def toggle_auth_file_status():
    body = request.get_json(silent=True) or {}
    node_name = str(body.get("node") or "").strip()
    auth_name = str(body.get("auth_id") or body.get("auth_name") or body.get("auth_index") or "").strip()
    disabled = body.get("disabled")
    if not node_name:
        return jsonify({"error": "node is required"}), 400
    if not auth_name:
        return jsonify({"error": "auth_id or auth_name is required"}), 400
    if not isinstance(disabled, bool):
        return jsonify({"error": "disabled must be boolean"}), 400
    node = next((item for item in CLIPROXY_NODES if item.get("name") == node_name), None)
    if not node:
        return jsonify({"error": "node not found"}), 404
    data, err = call_management_api_node(node, "PATCH", "/v0/management/auth-files/status", {
        "name": auth_name,
        "disabled": disabled,
    }, timeout=30)
    if err:
        return jsonify({"error": err}), 502
    clear_auth_stats_cache()
    return jsonify({
        "status": "ok",
        "node": node_name,
        "auth_name": auth_name,
        "disabled": disabled,
        "upstream": data or {},
    })


@app.route("/api/keys")
def get_keys():
    """Get all registered keys and their status."""
    files = get_auth_files()
    keys = []

    for f in files:
        # Only show claude/anthropic provider keys
        provider = f.get("provider", f.get("type", ""))
        if provider not in ("claude", "anthropic"):
            continue

        email = f.get("email") or f.get("account") or f.get("label") or "Unknown"
        status = f.get("status", "")
        unavailable = f.get("unavailable", False)
        disabled = f.get("disabled", False)

        # Only truly disabled keys are expired
        # Unavailable is a temporary state (refreshing, rate limited, etc.)
        expired = disabled or status == "disabled"

        keys.append({
            "email": email,
            "path": f.get("path", ""),
            "expired": expired,
            "unavailable": unavailable,  # Separate field for temporary unavailability
            "status": status,
            "modified": f.get("modtime", f.get("updated_at", ""))
        })

    return jsonify({"keys": keys})


@app.route("/api/usage-history")
def get_usage_history():
    """Get historical usage data with aggregations."""
    data = get_usage_history_aggregated()

    # Also get current-day and hourly data from the lightweight live summary.
    api_data = get_cluster_usage_summary()
    usage = api_data.get("usage", {})
    apply_live_today_usage(data, usage)
    enrich_usage_breakdowns(data)
    data["tokens_by_hour"] = usage.get("tokens_by_hour", {})
    data["requests_by_hour"] = usage.get("requests_by_hour", {})
    data["success_by_hour"] = usage.get("success_by_hour", {})
    data["failure_by_hour"] = usage.get("failure_by_hour", {})
    data["avg_latency_ms_by_hour"] = usage.get("avg_latency_ms_by_hour", {})

    recent_hours, recent_hours_refreshing = get_recent_hours_cached(usage)
    data["recent_hours"] = recent_hours
    data["recent_hours_refreshing"] = recent_hours_refreshing

    return jsonify(data)


# ============================================================================
# User Keys API Routes
# ============================================================================

def reassign_key_email(api_key, new_email):
    user_data = load_user_keys()
    users = user_data.get("users", {})
    keys_index = user_data.get("keys", {})
    key_info = keys_index.get(api_key)
    if not key_info:
        return False, "Key 不存在", None

    old_email = key_info.get("email", "")
    label = key_info.get("label", "")
    created_at = key_info.get("created_at", datetime.utcnow().isoformat() + "Z")

    for user in users.values():
        user["api_keys"] = [key for key in user.get("api_keys", []) if key != api_key]

    if new_email not in users:
        users[new_email] = {
            "email": new_email,
            "name": new_email,
            "api_keys": [],
            "created_at": datetime.utcnow().isoformat() + "Z",
        }
    if api_key not in users[new_email].get("api_keys", []):
        users[new_email].setdefault("api_keys", []).append(api_key)

    for email in list(users.keys()):
        if email != new_email and not users[email].get("api_keys"):
            del users[email]

    keys_index[api_key] = {
        **key_info,
        "email": new_email,
        "label": label or new_email,
        "created_at": created_at,
    }

    pool = load_key_pool()
    if api_key in pool.get("assigned", {}):
        pool["assigned"][api_key] = new_email

    if not save_user_keys(user_data):
        return False, "保存用户 Key 数据失败", None
    if not save_key_pool(pool):
        return False, "保存 Key 池数据失败", None

    migrated_rows = db.reassign_user_usage_key(api_key, new_email)
    return True, None, {"old_email": old_email, "new_email": new_email, "migrated_rows": migrated_rows}


@app.route("/api/register-key", methods=["POST"])
def register_key():
    """Register a new user and assign an API key."""
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    name = data.get("name", "").strip()
    label = data.get("label", "").strip()
    model_group = normalize_model_group(data.get("model_group", "common"))

    if not email:
        return jsonify({"error": "请输入邮箱"}), 400
    if not is_valid_email(email):
        return jsonify({"error": "请输入有效的邮箱"}), 400
    if not email.endswith("@zilliz.com"):
        return jsonify({"error": "申请 Key 请使用 zilliz.com 公司邮箱"}), 400

    if not name:
        name = email
    if not label:
        label = name or email

    # Assign key
    api_key, error = assign_key_to_user(email, name, label, model_group)

    if error:
        return jsonify({"error": error}), 500

    return jsonify({
        "success": True,
        "api_key": api_key,
        "identifier": name,
        "email": email,
        "model_group": model_group,
        "message": "API Key 申请成功！"
    })


@app.route("/api/update-key-email", methods=["POST"])
def update_key_email():
    data = request.get_json() or {}
    api_key = data.get("api_key", "").strip()
    email = data.get("email", "").strip().lower()

    if not api_key:
        return jsonify({"error": "请提供 API Key"}), 400
    if not email:
        return jsonify({"error": "请输入邮箱"}), 400
    if not is_valid_email(email):
        return jsonify({"error": "请输入有效的邮箱"}), 400

    success, error, result = reassign_key_email(api_key, email)
    if not success:
        return jsonify({"error": error}), 400

    return jsonify({"success": True, **result})


@app.route("/api/my-keys", methods=["POST"])
def get_my_keys():
    """Get all keys for a user by email."""
    data = request.get_json()
    email = data.get("email", "").strip().lower()

    if not email:
        return jsonify({"error": "请输入邮箱"}), 400

    user_data = load_user_keys()
    user = user_data["users"].get(email)

    if not user:
        return jsonify({"error": "未找到该用户"}), 404

    # Get stats for each key
    stats_data, _ = get_usage_stats_cached()
    apis = stats_data.get("usage", {}).get("apis", {}) if stats_data else {}

    keys_info = []
    for api_key in user.get("api_keys", []):
        key_meta = user_data["keys"].get(api_key, {})
        key_stats = apis.get(api_key, {})

        keys_info.append({
            "key": api_key,
            "label": key_meta.get("label", ""),
            "created_at": key_meta.get("created_at", ""),
            "total_requests": key_stats.get("total_requests", 0),
            "total_tokens": key_stats.get("total_tokens", 0)
        })

    return jsonify({
        "email": email,
        "name": user.get("name", email),
        "keys": keys_info
    })


@app.route("/api/revoke-key", methods=["POST"])
def revoke_key_api():
    """Revoke a user's API key."""
    data = request.get_json()
    api_key = data.get("key", "").strip()

    if not api_key:
        return jsonify({"error": "请提供 API Key"}), 400

    success, error = revoke_key(api_key)

    if error:
        return jsonify({"error": error}), 500

    return jsonify({
        "success": True,
        "message": "Key 已撤销"
    })


@app.route("/api/user-stats/<email>")
def api_get_user_stats(email):
    """Get detailed statistics for a specific user."""
    email = email.strip().lower()

    stats = get_user_stats(email)

    if not stats:
        return jsonify({"error": "用户不存在"}), 404

    return jsonify(stats)


def build_all_users_stats_response(aggregation, live_today=False):
    if aggregation == "day":
        stats = get_all_users_stats_by_period("day", live_today=live_today)
    elif aggregation == "month":
        stats = get_all_users_stats_by_period("month")
    elif aggregation == "year":
        stats = get_all_users_stats_by_period("year")
    else:
        aggregation = "total"
        stats = get_all_users_total_stats_from_db()

    total_users = len(set(s.get("email", "") for s in stats))
    total_requests = sum(s.get("total_requests", 0) for s in stats)
    total_tokens = sum(s.get("total_tokens", 0) for s in stats)
    input_tokens = sum(s.get("input_tokens", 0) for s in stats)
    output_tokens = sum(s.get("output_tokens", 0) for s in stats)
    cached_tokens = sum(s.get("cached_tokens", 0) for s in stats)
    reasoning_tokens = sum(s.get("reasoning_tokens", 0) for s in stats)
    token_breakdown = build_token_breakdown(total_tokens, input_tokens, output_tokens, cached_tokens, reasoning_tokens)
    if aggregation in ("day", "month", "year"):
        unique_keys = {
            api_key
            for stat in stats
            for api_key in stat.get("_api_keys", [])
            if api_key
        }
        total_keys = len(unique_keys)
        for stat in stats:
            stat.pop("_api_keys", None)
    else:
        unique_keys = {
            key.get("key")
            for stat in stats
            for key in stat.get("keys", [])
            if key.get("key")
        }
        total_keys = len(unique_keys)

    return {
        "users": stats,
        "summary": {
            "total_users": total_users,
            "total_requests": total_requests,
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "token_breakdown": token_breakdown,
            "estimated_cost_usd": token_breakdown["cost_usd"],
            "total_keys": total_keys,
        },
        "aggregation": aggregation,
        "token_pricing": {
            "usd_per_1m": TOKEN_PRICING_USD_PER_1M,
            "note": "按 GPT-5.5 API 标准价估算：input $5/1M，cached input $0.5/1M，output $30/1M；reasoning token 通常包含在 output 中，不单独计价。",
        },
    }


@app.route("/api/all-users-stats")
def api_get_all_users_stats():
    """Get statistics for all users with aggregation options."""
    aggregation = request.args.get("aggregation", "month").strip()
    live_today = request.args.get("live_today", "").strip() == "1"
    if aggregation not in ("total", "day", "month", "year"):
        aggregation = "total"
    return jsonify(build_all_users_stats_response(aggregation, live_today=live_today))


@app.route("/api/key-pool-status")
def key_pool_status():
    """Get key pool status."""
    pool = load_key_pool()

    return jsonify({
        "total": pool.get("total", len(pool.get("unused", [])) + len(pool.get("assigned", {}))),
        "unused": len(pool.get("unused", [])),
        "assigned": len(pool.get("assigned", {}))
    })


@app.route("/api/query-by-key", methods=["POST"])
def query_by_key():
    """Query user info by API key."""
    data = request.get_json()
    api_key = data.get("api_key", "").strip()

    if not api_key:
        return jsonify({"error": "请提供 API Key"}), 400

    user_data = load_user_keys()

    # Find which user owns this key
    key_info = user_data["keys"].get(api_key)
    if not key_info:
        return jsonify({"error": "Key 不存在"}), 404

    identifier = key_info["email"]
    user = user_data["users"].get(identifier)

    if not user:
        return jsonify({"error": "用户不存在"}), 404

    # Get stats for all keys of this user.
    # Keep top-level totals scoped to the queried key so older cached pages
    # still show per-key usage in the "My keys" history cards.
    stats_data, _ = get_usage_stats_cached()
    apis = stats_data.get("usage", {}).get("apis", {}) if stats_data else {}

    today = beijing_today()
    user_total_requests = 0
    user_total_tokens = 0
    user_total_input_tokens = 0
    user_total_output_tokens = 0
    user_total_cached_tokens = 0
    user_total_reasoning_tokens = 0
    user_today_requests = 0
    user_today_tokens = 0
    user_today_input_tokens = 0
    user_today_output_tokens = 0
    user_today_cached_tokens = 0
    user_today_reasoning_tokens = 0
    queried_key_requests = 0
    queried_key_tokens = 0
    queried_key_input_tokens = 0
    queried_key_output_tokens = 0
    queried_key_cached_tokens = 0
    queried_key_reasoning_tokens = 0
    queried_key_today_requests = 0
    queried_key_today_tokens = 0
    queried_key_today_input_tokens = 0
    queried_key_today_output_tokens = 0
    queried_key_today_cached_tokens = 0
    queried_key_today_reasoning_tokens = 0
    all_keys = []
    today_key_db_stats = db.get_user_key_usage_for_date(identifier, today)

    for key in user.get("api_keys", []):
        key_meta = user_data["keys"].get(key, {})
        key_stats = apis.get(key, {})
        today_stats = key_usage_for_date(key_stats, today)

        key_db_rows = db.get_user_key_usage_range(identifier, key, "1970-01-01", today)
        db_total_requests = sum(int(row.get("requests", 0) or 0) for row in key_db_rows)
        db_total_tokens = sum(int(row.get("total_tokens", 0) or 0) for row in key_db_rows)
        db_input_tokens = sum(int(row.get("input_tokens", 0) or 0) for row in key_db_rows)
        db_output_tokens = sum(int(row.get("output_tokens", 0) or 0) for row in key_db_rows)
        db_cached_tokens = sum(int(row.get("cached_tokens", 0) or 0) for row in key_db_rows)
        db_reasoning_tokens = sum(int(row.get("reasoning_tokens", 0) or 0) for row in key_db_rows)
        today_db_stats = today_key_db_stats.get(key, {})

        requests = max(int(key_stats.get("total_requests", 0) or 0), db_total_requests)
        tokens = max(int(key_stats.get("total_tokens", 0) or 0), db_total_tokens)
        input_tokens = db_input_tokens
        output_tokens = db_output_tokens
        cached_tokens = db_cached_tokens
        reasoning_tokens = db_reasoning_tokens
        today_requests = max(int(today_stats.get("total_requests", 0) or 0), int(today_db_stats.get("total_requests", 0) or 0))
        today_tokens = max(int(today_stats.get("total_tokens", 0) or 0), int(today_db_stats.get("total_tokens", 0) or 0))
        today_input_tokens = max(int(today_stats.get("input_tokens", 0) or 0), int(today_db_stats.get("input_tokens", 0) or 0))
        today_output_tokens = max(int(today_stats.get("output_tokens", 0) or 0), int(today_db_stats.get("output_tokens", 0) or 0))
        today_cached_tokens = max(int(today_stats.get("cached_tokens", 0) or 0), int(today_db_stats.get("cached_tokens", 0) or 0))
        today_reasoning_tokens = max(int(today_stats.get("reasoning_tokens", 0) or 0), int(today_db_stats.get("reasoning_tokens", 0) or 0))
        breakdown = build_token_breakdown(tokens, input_tokens, output_tokens, cached_tokens, reasoning_tokens)

        user_total_requests += requests
        user_total_tokens += tokens
        user_total_input_tokens += input_tokens
        user_total_output_tokens += output_tokens
        user_total_cached_tokens += cached_tokens
        user_total_reasoning_tokens += reasoning_tokens
        user_today_requests += today_requests
        user_today_tokens += today_tokens
        user_today_input_tokens += today_input_tokens
        user_today_output_tokens += today_output_tokens
        user_today_cached_tokens += today_cached_tokens
        user_today_reasoning_tokens += today_reasoning_tokens
        if key == api_key:
            queried_key_requests = requests
            queried_key_tokens = tokens
            queried_key_input_tokens = input_tokens
            queried_key_output_tokens = output_tokens
            queried_key_cached_tokens = cached_tokens
            queried_key_reasoning_tokens = reasoning_tokens
            queried_key_today_requests = today_requests
            queried_key_today_tokens = today_tokens
            queried_key_today_input_tokens = today_input_tokens
            queried_key_today_output_tokens = today_output_tokens
            queried_key_today_cached_tokens = today_cached_tokens
            queried_key_today_reasoning_tokens = today_reasoning_tokens

        all_keys.append({
            "key": key,
            "label": key_meta.get("label", ""),
            "model_group": key_meta.get("model_group", "common"),
            "created_at": key_meta.get("created_at", ""),
            "total_requests": requests,
            "total_tokens": tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "token_breakdown": breakdown,
            "estimated_cost_usd": breakdown["cost_usd"],
            "today_requests": today_requests,
            "today_tokens": today_tokens,
            "today_input_tokens": today_input_tokens,
            "today_output_tokens": today_output_tokens,
            "today_cached_tokens": today_cached_tokens,
            "today_reasoning_tokens": today_reasoning_tokens,
            "today_token_breakdown": build_token_breakdown(today_tokens, today_input_tokens, today_output_tokens, today_cached_tokens, today_reasoning_tokens),
        })

    queried_breakdown = build_token_breakdown(queried_key_tokens, queried_key_input_tokens, queried_key_output_tokens, queried_key_cached_tokens, queried_key_reasoning_tokens)
    user_breakdown = build_token_breakdown(user_total_tokens, user_total_input_tokens, user_total_output_tokens, user_total_cached_tokens, user_total_reasoning_tokens)
    return jsonify({
        "identifier": identifier,
        "date": today,
        "total_requests": queried_key_requests,
        "total_tokens": queried_key_tokens,
        "input_tokens": queried_key_input_tokens,
        "output_tokens": queried_key_output_tokens,
        "cached_tokens": queried_key_cached_tokens,
        "reasoning_tokens": queried_key_reasoning_tokens,
        "token_breakdown": queried_breakdown,
        "estimated_cost_usd": queried_breakdown["cost_usd"],
        "today_requests": queried_key_today_requests,
        "today_tokens": queried_key_today_tokens,
        "today_input_tokens": queried_key_today_input_tokens,
        "today_output_tokens": queried_key_today_output_tokens,
        "today_cached_tokens": queried_key_today_cached_tokens,
        "today_reasoning_tokens": queried_key_today_reasoning_tokens,
        "today_token_breakdown": build_token_breakdown(queried_key_today_tokens, queried_key_today_input_tokens, queried_key_today_output_tokens, queried_key_today_cached_tokens, queried_key_today_reasoning_tokens),
        "user_total_requests": user_total_requests,
        "user_total_tokens": user_total_tokens,
        "user_input_tokens": user_total_input_tokens,
        "user_output_tokens": user_total_output_tokens,
        "user_cached_tokens": user_total_cached_tokens,
        "user_reasoning_tokens": user_total_reasoning_tokens,
        "user_token_breakdown": user_breakdown,
        "user_estimated_cost_usd": user_breakdown["cost_usd"],
        "user_today_requests": user_today_requests,
        "user_today_tokens": user_today_tokens,
        "user_today_input_tokens": user_today_input_tokens,
        "user_today_output_tokens": user_today_output_tokens,
        "user_today_cached_tokens": user_today_cached_tokens,
        "user_today_reasoning_tokens": user_today_reasoning_tokens,
        "user_today_token_breakdown": build_token_breakdown(user_today_tokens, user_today_input_tokens, user_today_output_tokens, user_today_cached_tokens, user_today_reasoning_tokens),
        "all_keys": all_keys,
        "token_pricing": {
            "usd_per_1m": TOKEN_PRICING_USD_PER_1M,
            "note": "按 GPT-5.5 API 标准价估算：input $5/1M，cached input $0.5/1M，output $30/1M；reasoning token 通常包含在 output 中，不单独计价。",
        },
    })


@app.route("/api/user-keys")
def api_get_user_keys():
    email = request.args.get("email", "").strip()
    date = request.args.get("date", "").strip()

    if not email:
        return jsonify({"error": "请提供用户标识"}), 400
    if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400

    user_data = load_user_keys()
    user = user_data.get("users", {}).get(email)
    date_stats = db.get_user_key_usage_for_date(email, date) if date else {}
    key_entries = find_user_key_entries(user_data, email)

    if not user and not date_stats and not key_entries:
        return jsonify({"error": "用户不存在"}), 404

    api_keys = [entry.get("key", "") for entry in key_entries] or list(date_stats.keys())
    if date:
        stats_by_key = date_stats
    else:
        stats_data, _ = get_usage_stats_cached()
        stats_by_key = (stats_data.get("usage", {}).get("apis", {}) if stats_data else {})

    keys_info = []
    for api_key in api_keys:
        key_meta = user_data.get("keys", {}).get(api_key, {})
        key_stats = stats_by_key.get(api_key, {})
        breakdown = build_token_breakdown(key_stats.get("total_tokens", 0), key_stats.get("input_tokens", 0), key_stats.get("output_tokens", 0), key_stats.get("cached_tokens", 0), key_stats.get("reasoning_tokens", 0))
        keys_info.append({
            "key": api_key,
            "label": key_meta.get("label", ""),
            "model_group": key_meta.get("model_group", "common"),
            "created_at": key_meta.get("created_at", ""),
            "total_requests": key_stats.get("total_requests", 0),
            "success_count": key_stats.get("success_count", 0),
            "failure_count": key_stats.get("failure_count", 0),
            "total_tokens": key_stats.get("total_tokens", 0),
            "input_tokens": key_stats.get("input_tokens", 0),
            "output_tokens": key_stats.get("output_tokens", 0),
            "cached_tokens": key_stats.get("cached_tokens", 0),
            "reasoning_tokens": key_stats.get("reasoning_tokens", 0),
            "token_breakdown": breakdown,
            "estimated_cost_usd": breakdown["cost_usd"],
        })

    keys_info.sort(key=lambda item: item.get("total_tokens", 0), reverse=True)
    return jsonify({
        "email": email,
        "name": (user or {}).get("name", email),
        "date": date,
        "keys": keys_info,
    })


@app.route("/api/user-key-timeseries")
def api_get_user_key_timeseries():
    email = request.args.get("email", "").strip()
    api_key = request.args.get("api_key", "").strip()
    date_from = request.args.get("date_from", "").strip()
    date_to = request.args.get("date_to", "").strip()
    date = request.args.get("date", "").strip()

    if not api_key:
        return jsonify({"error": "请提供 API Key"}), 400

    if date_from and date_to:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date_from) or not re.match(r"^\d{4}-\d{2}-\d{2}$", date_to):
            return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400
    elif date:
        if not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
            return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400
        date_from = date
        date_to = date
    else:
        date_from = beijing_today()
        date_to = beijing_today()

    if date_from > date_to:
        date_from, date_to = date_to, date_from

    dt_from = datetime.strptime(date_from, "%Y-%m-%d")
    dt_to = datetime.strptime(date_to, "%Y-%m-%d")
    num_days = (dt_to - dt_from).days + 1
    if num_days > 90:
        return jsonify({"error": "时间范围不能超过 90 天"}), 400

    user_data = load_user_keys()
    key_info = user_data.get("keys", {}).get(api_key, {})
    owner = key_info.get("email", email)
    if email and owner and owner != email and owner.lower() != email.lower():
        return jsonify({"error": "该 Key 不属于该用户"}), 403

    cache_key = f"{owner or email}|{api_key}|{date_from}|{date_to}"
    now = time.time()
    with _user_key_timeseries_cache_lock:
        cached = _user_key_timeseries_cache["data"].get(cache_key)
        if cached and (now - cached["last_update"]) < _user_key_timeseries_cache["ttl"]:
            return jsonify(cached["data"])

    if not key_info:
        return jsonify({"error": "Key 不存在"}), 404

    rows_by_date = {
        row["date"]: row
        for row in db.get_user_key_usage_range(owner, api_key, date_from, date_to)
    }
    buckets = []
    d = dt_from
    while d <= dt_to:
        ds = d.strftime("%Y-%m-%d")
        row = rows_by_date.get(ds, {})
        total_tokens = int(row.get("total_tokens", 0) or 0)
        input_tokens = int(row.get("input_tokens", 0) or 0)
        output_tokens = int(row.get("output_tokens", 0) or 0)
        cached_tokens = int(row.get("cached_tokens", 0) or 0)
        reasoning_tokens = int(row.get("reasoning_tokens", 0) or 0)
        breakdown = build_token_breakdown(total_tokens, input_tokens, output_tokens, cached_tokens, reasoning_tokens)
        buckets.append({
            "date": ds,
            "requests": int(row.get("requests", 0) or 0),
            "success_count": int(row.get("success_count", 0) or 0),
            "failure_count": int(row.get("failure_count", 0) or 0),
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "token_breakdown": breakdown,
            "estimated_cost_usd": breakdown["cost_usd"],
        })
        d += timedelta(days=1)
    mode = "daily"

    totals = {
        "requests": sum(b["requests"] for b in buckets),
        "success_count": sum(b["success_count"] for b in buckets),
        "failure_count": sum(b["failure_count"] for b in buckets),
        "total_tokens": sum(b["total_tokens"] for b in buckets),
        "input_tokens": sum(b["input_tokens"] for b in buckets),
        "output_tokens": sum(b["output_tokens"] for b in buckets),
        "cached_tokens": sum(b["cached_tokens"] for b in buckets),
        "reasoning_tokens": sum(b["reasoning_tokens"] for b in buckets),
    }
    totals_breakdown = build_token_breakdown(totals["total_tokens"], totals["input_tokens"], totals["output_tokens"], totals["cached_tokens"], totals["reasoning_tokens"])
    totals["token_breakdown"] = totals_breakdown
    totals["estimated_cost_usd"] = totals_breakdown["cost_usd"]

    result = {
        "email": owner,
        "key": api_key,
        "label": key_info.get("label", ""),
        "date_from": date_from,
        "date_to": date_to,
        "date": date_from if num_days == 1 else None,
        "mode": mode,
        "timezone": "Asia/Shanghai",
        "buckets": buckets,
        "totals": totals,
        "token_pricing": {
            "usd_per_1m": TOKEN_PRICING_USD_PER_1M,
            "note": "按 GPT-5.5 API 标准价估算：input $5/1M，cached input $0.5/1M，output $30/1M；reasoning token 通常包含在 output 中，不单独计价。",
        },
    }
    with _user_key_timeseries_cache_lock:
        _user_key_timeseries_cache["data"][cache_key] = {
            "data": result,
            "last_update": time.time(),
        }
    return jsonify(result)


# WebSocket event handlers
@socketio.on("connect")
def handle_connect():
    """Handle client connection."""
    print(f"[WebSocket] Client connected")
    # Send current usage immediately
    broadcast_usage_update()


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection."""
    print(f"[WebSocket] Client disconnected")


@app.route("/api/sync-usage", methods=["POST"])
def trigger_sync_usage():
    """Manually trigger usage data sync."""
    success = sync_usage_from_api()
    return jsonify({"success": success})


@app.route("/api/accounts")
def get_accounts():
    """Get all accounts from user_mapping.json with their key status."""
    mapping = load_user_mapping()
    users = mapping.get("users", [])
    files = get_auth_files()
    now = datetime.utcnow()

    accounts = []
    for user in users:
        claude_email = user.get("claude_email", "")
        if not claude_email:
            continue

        account = {
            "name": user.get("name", claude_email),
            "claude_email": claude_email,
            "feishu_email": user.get("feishu_email", claude_email),
            "status": "no_key",  # no_key, active, expired
            "expires_at": None,
            "hours_left": None
        }

        # Check if user has contributed a key
        for f in files:
            email = f.get("email") or f.get("account") or ""
            if email.lower() == claude_email.lower():
                # User has a key file
                status = f.get("status", "")
                unavailable = f.get("unavailable", False)
                disabled = f.get("disabled", False)

                # Only truly disabled keys are expired
                # Unavailable is a temporary state
                if disabled or status == "disabled":
                    account["status"] = "expired"
                else:
                    account["status"] = "active"

                # Get expiry time
                detail = get_auth_file_detail(f.get("path", ""))
                if detail:
                    expires_at = detail.get("expired") or detail.get("expires_at")
                    if expires_at:
                        try:
                            exp_time = datetime.fromisoformat(expires_at.replace("Z", "+00:00").replace("+00:00", ""))
                            hours_left = (exp_time - now).total_seconds() / 3600
                            account["expires_at"] = expires_at
                            account["hours_left"] = round(hours_left, 1)

                            # Update status based on expiry time
                            # But give 1 hour grace period for auto-refresh
                            if hours_left <= -1:
                                account["status"] = "expired"
                        except Exception:
                            pass
                break

        accounts.append(account)

    return jsonify({"accounts": accounts})


@app.route("/api/send-notification", methods=["POST"])
def send_manual_notification():
    """Manually send notification to a user."""
    body = request.get_json()
    email = body.get("email", "")
    notification_type = body.get("type", "")  # remind_contribute, remind_renew

    if not email or not notification_type:
        return jsonify({"error": "Missing email or type"}), 400

    feishu_id = get_feishu_id(email)
    user_name = get_user_name(email)

    if notification_type == "remind_contribute":
        success = send_feishu_notification(
            feishu_id,
            "💡 邀请分享 Claude Key",
            f"Hi **{user_name}**，\n\n"
            f"我们诚邀您分享一个 Claude Key 到共享池，让团队成员都能使用 Claude AI。\n\n"
            f"**操作步骤**：\n"
            f"1. 访问 {_login_url()}\n"
            f"2. 点击「打开 Claude 授权」\n"
            f"3. 完成授权即可\n\n"
            f"完成授权即可"
        )
    elif notification_type == "remind_renew":
        success = send_feishu_notification(
            feishu_id,
            "🔄 Claude Key 已过期，请重新激活",
            f"Hi **{user_name}**，\n\n"
            f"您的 Claude Key 已过期，需要重新激活。\n\n"
            f"**重新激活步骤**：\n"
            f"1. 访问 {_login_url()}\n"
            f"2. 点击「打开 Claude 授权」\n"
            f"3. 完成授权即可\n\n"
            f"谢谢！"
        )
    else:
        return jsonify({"error": "Invalid notification type"}), 400

    if success:
        print(f"[Notification] Sent {notification_type} to {email}")
        return jsonify({"message": "通知已发送", "success": True})
    else:
        return jsonify({"error": "发送失败", "success": False}), 500


@app.route("/api/check-expiry")
def check_expiry():
    """Check for expiring keys and send notifications."""
    files = get_auth_files()
    expiring = []
    now = datetime.utcnow()

    for f in files:
        if not f.get("name", "").endswith(".json"):
            continue

        detail = get_auth_file_detail(f.get("path", ""))
        if not detail:
            continue

        expires_at = detail.get("expires_at", "")
        if not expires_at:
            continue

        try:
            # Parse expiry time
            exp_time = datetime.fromisoformat(expires_at.replace("Z", "+00:00").replace("+00:00", ""))
            hours_left = (exp_time - now).total_seconds() / 3600

            if hours_left <= config.KEY_EXPIRE_WARNING_HOURS:
                name = f.get("name", "")
                email = name.replace(".json", "") if "@" in name else "Unknown"

                expiring.append({
                    "email": email,
                    "hours_left": round(hours_left, 1),
                    "expires_at": expires_at
                })

                # Send notification
                feishu_id = get_feishu_id(email)
                send_feishu_notification(
                    feishu_id,
                    "Claude Key Expiring Soon",
                    f"**{email}** 's Claude key will expire in **{round(hours_left, 1)} hours**.\n\n"
                    f"Please visit {_login_url()} to re-authenticate."
                )
        except Exception as e:
            print(f"Error processing expiry for {f.get('name')}: {e}")

    return jsonify({
        "checked_at": now.isoformat(),
        "expiring_keys": expiring
    })


# Scheduler for periodic expiry checks
scheduler = BackgroundScheduler()


def scheduled_expiry_check():
    """Scheduled task to check key expiry."""
    with app.app_context():
        print(f"[Scheduler] Running expiry check at {datetime.utcnow().isoformat()}")
        try:
            files = get_auth_files()
            now = datetime.utcnow()

            for f in files:
                if not f.get("name", "").endswith(".json"):
                    continue

                detail = get_auth_file_detail(f.get("path", ""))
                if not detail or detail.get("expired"):
                    continue

                expires_at = detail.get("expires_at", "")
                if not expires_at:
                    continue

                try:
                    exp_time = datetime.fromisoformat(expires_at.replace("Z", "+00:00").replace("+00:00", ""))
                    hours_left = (exp_time - now).total_seconds() / 3600

                    if 0 < hours_left <= config.KEY_EXPIRE_WARNING_HOURS:
                        name = f.get("name", "")
                        email = name.replace(".json", "") if "@" in name else "Unknown"

                        feishu_id = get_feishu_id(email)
                        send_feishu_notification(
                            feishu_id,
                            "Claude Key Expiring Soon",
                            f"**{email}** 's Claude key will expire in **{round(hours_left, 1)} hours**.\n\n"
                            f"Please visit {_login_url()} to re-authenticate."
                        )
                        print(f"[Scheduler] Notified {email} - key expires in {hours_left:.1f}h")
                except Exception as e:
                    print(f"[Scheduler] Error processing {f.get('name')}: {e}")
        except Exception as e:
            print(f"[Scheduler] Error in expiry check: {e}")


def broadcast_usage_update():
    """Broadcast usage update to all connected WebSocket clients."""
    global _last_usage_state
    try:
        data, err = get_usage_summary_cached()
        if err:
            return

        current_tokens = data.get("total_tokens", 0)
        current_requests = data.get("total_requests", 0)

        # Cluster mode aggregates multiple nodes; per-node counters can move independently
        # as auth files migrate, so the single-node restart recovery heuristic is disabled.

        # Only broadcast if there's a change
        if (current_tokens != _last_usage_state["total_tokens"] or
            current_requests != _last_usage_state["total_requests"]):
            _last_usage_state["total_tokens"] = current_tokens
            _last_usage_state["total_requests"] = current_requests

            socketio.emit("usage_update", {
                "total_tokens": current_tokens,
                "total_requests": current_requests,
                "today_tokens": data.get("today_tokens", 0),
                "today_requests": data.get("today_requests", 0),
                "success_count": data.get("success_count", 0),
                "failure_count": data.get("failure_count", 0),
                "timestamp": datetime.now().isoformat()
            })
            print(f"[WebSocket] Broadcast usage update: {current_tokens:,} tokens")
    except Exception as e:
        print(f"[WebSocket] Error broadcasting: {e}")
        import traceback
        traceback.print_exc()


def scheduled_usage_sync():
    """Scheduled task to sync usage data to CSV and broadcast updates."""
    with app.app_context():
        print(f"[Scheduler] Running usage sync at {datetime.now().isoformat()}")
        sync_usage_from_api()
        broadcast_usage_update()


def scheduled_git_sync():
    """Scheduled task to push usage CSV to GitHub."""
    with app.app_context():
        print(f"[Scheduler] Running git sync at {datetime.now().isoformat()}")
        git_sync_usage_csv()


if __name__ == "__main__":
    # Load data on startup
    print("[Startup] Initializing database...")
    db.init_database()

    print("[Startup] Loading user keys database...")
    load_user_keys()

    print("[Startup] Loading usage history...")
    load_usage_history()

    print("[Startup] Restoring CLIProxyAPI usage snapshots...")
    # import_cliproxy_snapshot()  # disabled: NLB architecture, nodes are independent

    # Start scheduler
    scheduler.add_job(
        scheduled_expiry_check,
        "interval",
        minutes=config.KEY_CHECK_INTERVAL_MINUTES,
        id="expiry_check"
    )

    # Sync usage every 5 minutes
    scheduler.add_job(
        scheduled_usage_sync,
        "interval",
        minutes=5,
        id="usage_sync"
    )

    # Git sync daily at 00:05
    scheduler.add_job(
        scheduled_git_sync,
        "cron",
        hour=0,
        minute=5,
        id="git_sync"
    )

    # Export full CLIProxyAPI snapshots infrequently; the payloads are large.
    scheduler.add_job(
        scheduled_snapshot_export,
        "interval",
        minutes=60,
        id="snapshot_export"
    )

    # Real-time usage broadcast. Keep this modest: management usage payloads
    # grow with traffic and can create large short-lived objects.
    scheduler.add_job(
        broadcast_usage_update,
        "interval",
        seconds=15,
        id="usage_broadcast",
        max_instances=1,
        coalesce=True
    )

    scheduler.start()
    print(f"[Scheduler] Started:")
    print(f"  - Expiry check: every {config.KEY_CHECK_INTERVAL_MINUTES} min")
    print(f"  - Usage sync:   every 5 min")
    print(f"  - Git sync:     daily at 00:05")
    print(f"  - Snapshot:     every 60 min")
    print(f"  - Broadcast:    every 15 sec")

    # Initial sync
    sync_usage_from_api()

    # Run Flask app with SocketIO
    print(f"Starting Key Portal on {config.HOST}:{config.PORT} (WebSocket enabled)")
    socketio.run(app, host=config.HOST, port=config.PORT, debug=False, allow_unsafe_werkzeug=True)
