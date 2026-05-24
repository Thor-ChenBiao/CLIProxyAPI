#!/usr/bin/env python3
"""
CLIProxyAPI Key Portal
A web service for managing OAuth key contributions and monitoring key status.
"""

import csv
import json
import hashlib
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
from urllib.parse import urlparse, parse_qs, quote, urlencode, parse_qsl, urlsplit, urlunsplit
from flask import Flask, render_template, request, jsonify, redirect, url_for
from flask_socketio import SocketIO, emit
from apscheduler.schedulers.background import BackgroundScheduler

import config
import database as db
import portal_state
import portal_auth
import portal_docs
import portal_scheduler
import auth_stats_service
import status_events
import usage_realtime

# Import modular components
import snapshot
import user_keys
import feishu
import approval
import nlb_monitor
from routes import pages, websocket

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get('KEY_PORTAL_SECRET_KEY', 'key-portal-secret')
socketio = SocketIO(app, cors_allowed_origins="*")
portal_state.configure(config.KEY_PORTAL_DATABASE_URL, config.KEY_PORTAL_REDIS_URL)
usage_broadcaster = usage_realtime.UsageRealtimeBroadcaster(socketio, lambda: get_usage_summary_cached())

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

_user_usage_summary_cache = {
    "data": {},
    "ttl": 2,
}
_user_usage_summary_cache_lock = threading.Lock()

_persistent_floor_cache = {
    "data": None,
    "last_update": 0,
    "ttl": 5,
}
_persistent_floor_cache_lock = threading.Lock()

_litellm_usage_cache = {
    "data": None,
    "last_update": 0,
    "ttl": 10,
}
_litellm_usage_cache_lock = threading.Lock()

_usage_floor_delta_baseline = {
    "data": {},
}
_usage_floor_delta_baseline_lock = threading.Lock()

BEIJING_TZ = timezone(timedelta(hours=8))

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

_usage_history_response_cache = {
    "data": None,
    "last_update": 0,
    "ttl": 60,
    "refreshing": False,
}
_usage_history_response_cache_lock = threading.Lock()
USAGE_HISTORY_RESPONSE_CACHE_FILE = os.path.join(os.path.dirname(__file__), "data", "usage_history_cache.json")

# User keys cache and file path
USER_KEYS_FILE = os.path.join(os.path.dirname(__file__), "data", "user_keys.json")
KEY_POOL_FILE = os.path.join(os.path.dirname(__file__), "data", "key_pool.json")
MONITOR_LOG_DIR = os.environ.get("KEY_PORTAL_MONITOR_LOG_DIR", os.path.join(os.path.dirname(__file__), "..", "monitor-logs"))
ALLOWED_MODEL_GROUPS = {"common", "claude", "deepseek"}
SESSION_COOKIE_NAME = "kp_session"


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
    return f"{_public_base_url()}/"


def _safe_next_url(value, default="/"):
    next_url = str(value or "").strip()
    if not next_url.startswith("/") or next_url.startswith("//"):
        return default
    if next_url.startswith("/login"):
        return default
    return next_url


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

    pg_data = portal_state.load_user_keys()
    if pg_data:
        _user_keys_cache["data"] = pg_data
        _user_keys_cache["loaded"] = True
        print(f"[UserKeys] Loaded {len(pg_data.get('users', {}))} users, {len(pg_data.get('keys', {}))} keys from Postgres")
        return pg_data

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
    if portal_state.is_pg_enabled():
        if portal_state.save_user_keys(data):
            _user_keys_cache["data"] = data
            _user_keys_cache["loaded"] = True
            print(f"[UserKeys] Saved {len(data.get('users', {}))} users to Postgres")
            return True
        print("[UserKeys] Error saving to Postgres")
        return False

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
    pg_pool = portal_state.load_key_pool()
    if pg_pool:
        return pg_pool

    if os.path.exists(KEY_POOL_FILE):
        try:
            with open(KEY_POOL_FILE, "r") as f:
                return json.load(f)
        except Exception as e:
            print(f"[KeyPool] Error loading: {e}")
    return {"unused": [], "assigned": {}}


def save_key_pool(data):
    """Save key pool."""
    if portal_state.is_pg_enabled():
        return portal_state.save_key_pool(data)

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


def mask_api_key(api_key):
    text = str(api_key or "")
    if len(text) <= 12:
        return "[redacted]"
    return f"{text[:6]}...{text[-4:]}"


LITELLM_MODEL_GROUPS = {
    "common": {
        "models": [
            "gpt-5.5",
            "gpt-5.4",
            "claude-sonnet-4-5",
            "claude-opus-4-6",
            "claude-opus-4-7",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5-20250929",
            "claude-haiku-4-5-20251001",
            "claude-sonnet-4-20250514",
            "claude-opus-4-20250514",
            "claude-opus-4-1-20250805",
            "claude-opus-4-5-20251101",
            "claude-3-5-haiku-20241022",
        ],
        "key_type": "gpt",
    },
    "claude": {
        "models": [
            "claude-opus-4-6",
            "claude-sonnet-4-6",
            "claude-sonnet-4-5",
            "claude-haiku-4-5",
        ],
        "aliases": {
            "claude-opus-4-6": "bedrock-claude-opus-4-6",
            "claude-sonnet-4-6": "bedrock-claude-sonnet-4-6",
            "claude-sonnet-4-5": "bedrock-claude-sonnet-4-5",
            "claude-haiku-4-5": "bedrock-claude-haiku-4-5",
        },
        "key_type": "claude",
        "rpm_limit": 20,
        "tpm_limit": 200000,
        "max_budget": 1.0,
    },
    "gemini": {
        "models": ["gemini-3-pro-preview", "gemini-2.5-pro"],
        "key_type": "gemini",
        "rpm_limit": 30,
        "tpm_limit": 300000,
        "max_budget": 1.0,
    },
    "deepseek": {
        "models": ["deepseek-chat", "deepseek-reasoner"],
        "key_type": "deepseek",
        "rpm_limit": 30,
        "tpm_limit": 300000,
        "max_budget": 1.0,
    },
}


def save_user_key_assignment(api_key, email, name, label, model_group, source, max_budget=None):
    user_keys = load_user_keys()
    if email not in user_keys["users"]:
        user_keys["users"][email] = {
            "email": email,
            "name": name or email,
            "api_keys": [],
            "created_at": datetime.utcnow().isoformat() + "Z"
        }

    created_at = datetime.utcnow().isoformat() + "Z"
    user_keys["users"][email]["api_keys"].append(api_key)
    entry = {
        "email": email,
        "label": label or "默认",
        "model_group": model_group,
        "source": source,
        "created_at": created_at
    }
    if max_budget is not None:
        entry["max_budget"] = float(max_budget)
    user_keys["keys"][api_key] = entry
    save_user_keys(user_keys)


def issue_litellm_key(email, name, label, model_group, max_budget=None):
    group_config = LITELLM_MODEL_GROUPS[model_group]
    payload = {
        "models": group_config["models"],
        "user_id": email,
        "key_alias": f"{model_group}:{label or name or email}",
        "metadata": {
            "email": email,
            "name": name or email,
            "label": label or "默认",
            "model_group": model_group,
            "key_type": group_config["key_type"],
            "issuer": "key-portal",
        },
    }
    if group_config.get("aliases"):
        payload["aliases"] = group_config["aliases"]
    for field in ("rpm_limit", "tpm_limit", "max_budget"):
        if field in group_config:
            payload[field] = group_config[field]
    if max_budget is not None:
        payload["max_budget"] = max_budget

    try:
        resp = requests.post(
            f"{config.LITELLM_API_URL}/key/generate",
            headers={"Authorization": f"Bearer {config.LITELLM_MASTER_KEY}"},
            json=payload,
            timeout=30,
        )
        if resp.status_code != 200:
            return None, f"LiteLLM key generation failed: {resp.status_code}"
        data = resp.json()
        api_key = data.get("key") or data.get("token")
        if not api_key:
            return None, "LiteLLM key generation response did not include a key"
        clear_litellm_key_budget_duration(api_key)
        return api_key, None
    except Exception as e:
        return None, f"LiteLLM key generation failed: {e}"


def assign_key_to_user(email, name, label, model_group="common", max_budget=None):
    """Assign a new API key to user."""
    model_group = normalize_model_group(model_group)

    if config.LITELLM_ISSUE_KEYS and config.LITELLM_MASTER_KEY:
        api_key, error = issue_litellm_key(email, name, label, model_group, max_budget=max_budget)
        if error:
            return None, error
        save_user_key_assignment(api_key, email, name, label, model_group, "litellm", max_budget=max_budget)
        print(f"[UserKeys] Assigned LiteLLM key {mask_api_key(api_key)} to {email} ({model_group}) budget={max_budget}")
        return api_key, None

    pool = load_key_pool()
    if not pool.get("unused"):
        return None, "Key pool is empty, please generate more keys"

    api_key = pool["unused"].pop(0)
    pool["assigned"][api_key] = email
    save_key_pool(pool)
    save_user_key_assignment(api_key, email, name, label, model_group, "cliproxy")

    print(f"[UserKeys] Assigned pool key {mask_api_key(api_key)} to {email} ({model_group})")
    return api_key, None


def _litellm_token_hash(api_key):
    return hashlib.sha256(str(api_key or "").encode()).hexdigest()


def clear_litellm_key_budget_duration(api_key):
    sql = f"""
    WITH updated AS (
      UPDATE "LiteLLM_VerificationToken"
      SET budget_duration = NULL,
          budget_reset_at = NULL,
          updated_at = NOW()
      WHERE token = {_sql_literal(_litellm_token_hash(api_key))}
      RETURNING token
    )
    SELECT json_build_object('updated', count(*)) FROM updated
    """
    row = litellm_psql_json(sql, timeout=10)
    return isinstance(row, dict) and int(row.get("updated") or 0) > 0


def update_litellm_key_budget(api_key, max_budget):
    if not config.LITELLM_MASTER_KEY:
        return False, "LiteLLM master key is not configured"
    try:
        resp = requests.post(
            f"{config.LITELLM_API_URL}/key/update",
            headers={"Authorization": f"Bearer {config.LITELLM_MASTER_KEY}"},
            json={"key": api_key, "max_budget": float(max_budget)},
            timeout=30,
        )
        if resp.status_code == 200:
            clear_litellm_key_budget_duration(api_key)
            return True, None
        return False, f"LiteLLM key update failed: {resp.status_code}"
    except Exception as e:
        return False, f"LiteLLM key update failed: {e}"


def update_user_key_budget(api_key, max_budget):
    user_keys = load_user_keys()
    key_info = user_keys.get("keys", {}).get(api_key)
    if not key_info:
        return False
    key_info["max_budget"] = float(max_budget)
    key_info["updated_at"] = datetime.utcnow().isoformat() + "Z"
    return save_user_keys(user_keys)


def _parse_positive_budget(value):
    text = str(value or "").strip().replace("$", "").replace("￥", "").replace("元", "").replace("美元", "")
    try:
        amount = float(text)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(amount) or amount <= 0:
        return None
    return round(amount, 6)


def get_litellm_key_budget(api_key):
    sql = f"""
    SELECT json_build_object('max_budget', max_budget)
    FROM "LiteLLM_VerificationToken"
    WHERE token = {_sql_literal(api_key)} OR token = {_sql_literal(_litellm_token_hash(api_key))}
    LIMIT 1
    """
    row = litellm_psql_json(sql, timeout=5)
    if isinstance(row, dict):
        return _parse_positive_budget(row.get("max_budget"))
    return None


def _key_budget(key_info, api_key=None):
    if api_key:
        budget = get_litellm_key_budget(api_key)
        if budget is not None:
            return budget
    budget = _parse_positive_budget(key_info.get("max_budget"))
    if budget is not None:
        return budget
    group = normalize_model_group(key_info.get("model_group", "common"))
    return _parse_positive_budget(LITELLM_MODEL_GROUPS.get(group, {}).get("max_budget"))


def delete_litellm_key(api_key):
    if not config.LITELLM_MASTER_KEY:
        return False, "LiteLLM master key is not configured"
    try:
        resp = requests.post(
            f"{config.LITELLM_API_URL}/key/delete",
            headers={"Authorization": f"Bearer {config.LITELLM_MASTER_KEY}"},
            json={"keys": [api_key]},
            timeout=30,
        )
        if resp.status_code in (200, 404):
            return True, None
        return False, f"LiteLLM key delete failed: {resp.status_code}"
    except Exception as e:
        return False, f"LiteLLM key delete failed: {e}"


def revoke_key(api_key):
    """Revoke a key."""
    user_keys = load_user_keys()

    # Find key owner
    key_info = user_keys["keys"].get(api_key)
    if not key_info:
        return False, "Key not found"

    email = key_info["email"]
    source = key_info.get("source", "cliproxy")

    if source == "litellm":
        success, err = delete_litellm_key(api_key)
        if not success:
            return False, err

    # Remove from user
    if email in user_keys["users"] and api_key in user_keys["users"][email].get("api_keys", []):
        user_keys["users"][email]["api_keys"].remove(api_key)

    # Remove from keys index
    del user_keys["keys"][api_key]

    save_user_keys(user_keys)

    if source != "litellm":
        pool = load_key_pool()
        pool["unused"].append(api_key)
        if api_key in pool["assigned"]:
            del pool["assigned"][api_key]
        save_key_pool(pool)

        data, err = call_management_api("GET", "/v0/management/api-keys")
        if not err:
            keys = data.get("api_keys", [])
            if api_key in keys:
                keys.remove(api_key)
                call_management_api("PUT", "/v0/management/api-keys", keys)

    print(f"[UserKeys] Revoked {source} key {mask_api_key(api_key)} from {email}")
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


def _format_litellm_user_stat(row, user_keys_data, include_period=True, include_keys=False):
    users = user_keys_data.get("users", {})
    email = row.get("user_email", "") or "unknown"
    user_info = users.get(email, {})
    litellm_keys = [key for key in row.get("api_keys", []) or [] if key]
    registered_keys = [
        entry.get("key", "")
        for entry in find_user_key_entries(user_keys_data, email)
        if entry.get("key", "")
    ]
    display_keys = registered_keys or litellm_keys
    spend_usd = round(_float_usage_value(row.get("spend_usd")), 6)
    breakdown = build_litellm_token_breakdown(
        row.get("total_tokens", 0),
        row.get("input_tokens", 0),
        row.get("output_tokens", 0),
        row.get("cached_tokens", 0),
        row.get("reasoning_tokens", 0),
        spend_usd,
    )
    stat = {
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
        "spend_usd": spend_usd,
        "token_breakdown": breakdown,
        "estimated_cost_usd": breakdown["cost_usd"],
        "key_count": len(set(display_keys)) or row.get("num_keys", 0),
    }
    if include_period:
        stat["period"] = row.get("period", "total")
        stat["_api_keys"] = display_keys
    if include_keys:
        stat["keys"] = [{"key": key} for key in display_keys]
    return stat


def get_all_users_total_stats_from_db():
    user_keys_data = load_user_keys()
    rows = litellm_user_stats("total")
    if not rows:
        rows = db.get_all_users_total_usage()
    return [_format_litellm_user_stat(row, user_keys_data, include_period=False, include_keys=True) for row in rows]


def get_all_users_stats_by_period(period="month", live_today=False):
    """
    Get statistics for all users aggregated by period (month or year).
    Returns a list with each user's stats broken down by the selected period.
    """
    user_keys_data = load_user_keys()
    rows = litellm_user_stats(period)
    if not rows:
        rows = db.get_user_usage_by_period(period)
    return [_format_litellm_user_stat(row, user_keys_data, include_period=True, include_keys=False) for row in rows]


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
    {"name": "node-a", "url": "http://127.0.0.1:8317"},
    {"name": "node-b", "url": "http://172.31.26.28:8317"},
    {"name": "node-c", "url": "https://172.31.16.7"},
    {"name": "node-d", "url": "https://172.31.24.86"},
    {"name": "node-e", "url": "https://172.31.29.243"},
    {"name": "node-f", "url": "https://172.31.25.74"},
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
        verify_tls = not str(node.get("url", "")).startswith("https://172.31.")
        if method == "GET":
            resp = requests.get(url, headers=headers, timeout=timeout, verify=verify_tls)
        elif method == "POST":
            resp = requests.post(url, headers=headers, json=data, timeout=timeout, verify=verify_tls)
        elif method == "PATCH":
            resp = requests.patch(url, headers=headers, json=data, timeout=timeout, verify=verify_tls)
        else:
            return None, f"Unsupported method: {method}"
        if resp.status_code == 200:
            return resp.json(), None
        return None, f"API error: {resp.status_code} - {resp.text}"
    except Exception as e:
        return None, str(e)


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


def _sum_beijing_today(usage, today_str):
    tokens_by_day = usage.get("tokens_by_day") or {}
    requests_by_day = usage.get("requests_by_day") or {}
    return (
        int(tokens_by_day.get(today_str, 0) or 0),
        int(requests_by_day.get(today_str, 0) or 0),
    )


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
    today_breakdown = build_litellm_token_breakdown(
        summary.get("today_tokens", 0),
        summary.get("today_input_tokens", 0),
        summary.get("today_output_tokens", 0),
        summary.get("today_cached_tokens", 0),
        summary.get("today_reasoning_tokens", 0),
        summary.get("today_spend_usd", 0),
    )
    total_breakdown = build_litellm_token_breakdown(
        summary.get("total_tokens", 0),
        summary.get("input_tokens", 0),
        summary.get("output_tokens", 0),
        summary.get("cached_tokens", 0),
        summary.get("reasoning_tokens", 0),
        summary.get("spend_usd", 0),
    )
    summary["token_breakdown"] = {
        "today": today_breakdown,
        "total": total_breakdown,
        "pricing": litellm_spend_pricing_metadata(),
    }
    summary["estimated_cost_usd"] = total_breakdown["cost_usd"]
    summary["today_estimated_cost_usd"] = today_breakdown["cost_usd"]
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


def _int_usage_value(value):
    try:
        return int(value or 0)
    except Exception:
        return 0


def _float_usage_value(value):
    try:
        return float(value or 0)
    except Exception:
        return 0.0


def litellm_database_url():
    url = getattr(config, "LITELLM_DATABASE_URL", "") or os.environ.get("LITELLM_DATABASE_URL", "") or os.environ.get("DATABASE_URL", "")
    return str(url or "").strip()


def litellm_psycopg_database_url():
    text = litellm_database_url()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except Exception:
        return text
    if not parts.query:
        return text
    filtered = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "pgbouncer"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered), parts.fragment))


def litellm_psql_json(sql, timeout=15):
    database_url = litellm_psycopg_database_url()
    if not database_url:
        return None
    try:
        result = subprocess.run(
            ["psql", database_url, "-X", "-q", "-t", "-A", "-v", "ON_ERROR_STOP=1", "-c", sql],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout,
        )
        if result.returncode == 0:
            return json.loads((result.stdout or "").strip() or "null")
    except FileNotFoundError:
        pass
    except Exception:
        return None

    try:
        import psycopg
        with psycopg.connect(database_url, connect_timeout=max(1, min(int(timeout or 15), 30))) as conn:
            with conn.cursor() as cursor:
                cursor.execute(sql)
                row = cursor.fetchone()
                return row[0] if row else None
    except Exception as e:
        print(f"[LiteLLM] Database query unavailable: {e}")
        return None


def _sql_literal(value):
    return "'" + str(value or "").replace("'", "''") + "'"


def litellm_spend_identity_sql():
    return "coalesce(nullif(v.metadata->>'email', ''), nullif(v.user_id, ''), nullif(s.\"user\", ''), 'unknown')"


def litellm_cached_tokens_sql():
    return "coalesce((s.metadata->'usage_object'->'prompt_tokens_details'->>'cached_tokens')::bigint, 0)"


def build_litellm_token_breakdown(total_tokens, input_tokens=0, output_tokens=0, cached_tokens=0, reasoning_tokens=0, spend_usd=0):
    breakdown = build_token_breakdown(total_tokens, input_tokens, output_tokens, cached_tokens, reasoning_tokens)
    breakdown["cost_usd"] = round(_float_usage_value(spend_usd), 4)
    breakdown["spend_usd"] = round(_float_usage_value(spend_usd), 6)
    return breakdown


def litellm_spend_pricing_metadata():
    return {
        "source": "litellm_spendlogs",
        "note": "费用以 LiteLLM SpendLogs.spend 为准；该值由 LiteLLM 按模型价格表和 token usage 计算。",
    }


def litellm_usage_history_aggregated(days=120):
    sql = f"""
WITH rows AS (
    SELECT
        ((s."endTime" + interval '8 hours')::date)::text AS date,
        count(*)::bigint AS total_requests,
        count(*) FILTER (WHERE coalesce(s.status, 'success') != 'failure')::bigint AS success_count,
        count(*) FILTER (WHERE coalesce(s.status, 'success') = 'failure')::bigint AS failure_count,
        coalesce(sum(s.total_tokens), 0)::bigint AS total_tokens,
        coalesce(sum(s.prompt_tokens), 0)::bigint AS input_tokens,
        coalesce(sum(s.completion_tokens), 0)::bigint AS output_tokens,
        coalesce(sum({litellm_cached_tokens_sql()}), 0)::bigint AS cached_tokens,
        coalesce(sum((s.metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint), 0)::bigint AS reasoning_tokens,
        coalesce(sum(s.spend), 0)::float8 AS spend_usd
    FROM "LiteLLM_SpendLogs" s
    WHERE s."endTime" >= (current_date - interval '{int(days)} days')
    GROUP BY 1
), enriched AS (
    SELECT * FROM rows ORDER BY date
), month_rows AS (
    SELECT substr(date, 1, 7) AS period,
           sum(total_requests)::bigint AS total_requests,
           sum(success_count)::bigint AS success_count,
           sum(failure_count)::bigint AS failure_count,
           sum(total_tokens)::bigint AS total_tokens,
           sum(input_tokens)::bigint AS input_tokens,
           sum(output_tokens)::bigint AS output_tokens,
           sum(cached_tokens)::bigint AS cached_tokens,
           sum(reasoning_tokens)::bigint AS reasoning_tokens,
           sum(spend_usd)::float8 AS spend_usd
    FROM rows GROUP BY 1
), year_rows AS (
    SELECT substr(date, 1, 4) AS period,
           sum(total_requests)::bigint AS total_requests,
           sum(success_count)::bigint AS success_count,
           sum(failure_count)::bigint AS failure_count,
           sum(total_tokens)::bigint AS total_tokens,
           sum(input_tokens)::bigint AS input_tokens,
           sum(output_tokens)::bigint AS output_tokens,
           sum(cached_tokens)::bigint AS cached_tokens,
           sum(reasoning_tokens)::bigint AS reasoning_tokens,
           sum(spend_usd)::float8 AS spend_usd
    FROM rows GROUP BY 1
)
SELECT json_build_object(
    'history', coalesce((SELECT json_agg(row_to_json(enriched)) FROM enriched), '[]'::json),
    'by_month', coalesce((SELECT json_object_agg(period, row_to_json(month_rows)) FROM month_rows), '{{}}'::json),
    'by_year', coalesce((SELECT json_object_agg(period, row_to_json(year_rows)) FROM year_rows), '{{}}'::json)
);
"""
    data = litellm_psql_json(sql, timeout=30)
    if not data:
        return None
    return data


def litellm_usage_by_model_group(days=7):
    days = max(1, int(days or 7))
    sql = f"""
WITH rows AS (
    SELECT
        CASE
            WHEN lower(coalesce(v.metadata->>'model_group', '')) = 'claude' THEN 'claude'
            WHEN lower(coalesce(v.metadata->>'model_group', '')) = 'deepseek' THEN 'deepseek'
            WHEN lower(coalesce(v.metadata->>'model_group', '')) = 'gemini' THEN 'gemini'
            WHEN lower(coalesce(s.model, '')) LIKE 'openai/%' THEN 'gpt'
            WHEN lower(coalesce(s.model, '')) LIKE '%claude%' THEN 'claude'
            WHEN lower(coalesce(s.model, '')) LIKE '%deepseek%' THEN 'deepseek'
            WHEN lower(coalesce(s.model, '')) LIKE '%gemini%' THEN 'gemini'
            ELSE 'gpt'
        END AS model_group,
        count(*)::bigint AS requests,
        count(*) FILTER (WHERE coalesce(s.status, 'success') != 'failure')::bigint AS success_count,
        count(*) FILTER (WHERE coalesce(s.status, 'success') = 'failure')::bigint AS failure_count,
        coalesce(sum(s.total_tokens), 0)::bigint AS tokens,
        coalesce(sum(s.prompt_tokens), 0)::bigint AS input_tokens,
        coalesce(sum(s.completion_tokens), 0)::bigint AS output_tokens,
        coalesce(sum({litellm_cached_tokens_sql()}), 0)::bigint AS cached_tokens,
        coalesce(sum(s.spend), 0)::float8 AS spend_usd
    FROM "LiteLLM_SpendLogs" s
    LEFT JOIN "LiteLLM_VerificationToken" v ON s.api_key = v.token
    WHERE s."endTime" >= now() - interval '{days} days'
    GROUP BY 1
)
SELECT coalesce(json_agg(row_to_json(rows) ORDER BY tokens DESC), '[]'::json) FROM rows;
"""
    return litellm_psql_json(sql, timeout=15) or []


def litellm_recent_hours(hours=48):
    hours = max(1, int(hours or 48))
    sql = f"""
WITH bounds AS (
    SELECT date_trunc('hour', now() + interval '8 hours') - ((g || ' hours')::interval) AS hour_bj
    FROM generate_series({hours - 1}, 0, -1) AS g
), report_window AS (
    SELECT min(hour_bj) - interval '8 hours' AS start_utc,
           max(hour_bj) + interval '1 hour' - interval '8 hours' AS end_utc
    FROM bounds
), agg AS (
    SELECT
        date_trunc('hour', s."endTime" + interval '8 hours') AS hour_bj,
        count(*)::bigint AS total_requests,
        count(*)::bigint AS requests,
        count(*) FILTER (WHERE coalesce(s.status, 'success') != 'failure')::bigint AS success_count,
        count(*) FILTER (WHERE coalesce(s.status, 'success') = 'failure')::bigint AS failure_count,
        coalesce(sum(s.total_tokens), 0)::bigint AS total_tokens,
        coalesce(sum(s.total_tokens), 0)::bigint AS tokens,
        coalesce(sum(s.prompt_tokens), 0)::bigint AS input_tokens,
        coalesce(sum(s.completion_tokens), 0)::bigint AS output_tokens,
        coalesce(sum({litellm_cached_tokens_sql()}), 0)::bigint AS cached_tokens,
        coalesce(sum(s.spend), 0)::float8 AS spend_usd,
        coalesce(avg(extract(epoch FROM (s."endTime" - s."startTime")) * 1000), 0)::float8 AS avg_latency_ms
    FROM "LiteLLM_SpendLogs" s, report_window
    WHERE s."endTime" >= report_window.start_utc
      AND s."endTime" < report_window.end_utc
    GROUP BY 1
), rows AS (
    SELECT
        to_char(bounds.hour_bj, 'YYYY-MM-DD HH24:00') AS hour,
        to_char(bounds.hour_bj, 'MM-DD HH24:00') AS label,
        to_char(bounds.hour_bj, 'YYYY-MM-DD HH24:00') AS "utcKey",
        coalesce(agg.total_requests, 0)::bigint AS total_requests,
        coalesce(agg.requests, 0)::bigint AS requests,
        coalesce(agg.success_count, 0)::bigint AS success_count,
        coalesce(agg.failure_count, 0)::bigint AS failure_count,
        coalesce(agg.total_tokens, 0)::bigint AS total_tokens,
        coalesce(agg.tokens, 0)::bigint AS tokens,
        coalesce(agg.input_tokens, 0)::bigint AS input_tokens,
        coalesce(agg.output_tokens, 0)::bigint AS output_tokens,
        coalesce(agg.cached_tokens, 0)::bigint AS cached_tokens,
        coalesce(agg.spend_usd, 0)::float8 AS spend_usd,
        coalesce(agg.avg_latency_ms, 0)::float8 AS avg_latency_ms
    FROM bounds
    LEFT JOIN agg ON agg.hour_bj = bounds.hour_bj
    ORDER BY bounds.hour_bj
)
SELECT coalesce(json_agg(row_to_json(rows)), '[]'::json) FROM rows;
"""
    return litellm_psql_json(sql, timeout=15) or []


def litellm_user_stats(period="month"):
    if period not in ("day", "month", "year", "total"):
        period = "month"
    period_expr = {
        "day": "((s.\"endTime\" + interval '8 hours')::date)::text",
        "month": "to_char(s.\"endTime\" + interval '8 hours', 'YYYY-MM')",
        "year": "to_char(s.\"endTime\" + interval '8 hours', 'YYYY')",
        "total": "'total'",
    }[period]
    order_sql = "total_tokens DESC" if period == "total" else "period DESC, total_tokens DESC"
    identity = litellm_spend_identity_sql()
    sql = f"""
WITH rows AS (
    SELECT
        {period_expr} AS period,
        {identity} AS user_email,
        coalesce(nullif(v.key_alias, ''), left(s.api_key, 16)) AS key_label,
        count(*)::bigint AS total_requests,
        count(*) FILTER (WHERE coalesce(s.status, 'success') != 'failure')::bigint AS success_count,
        count(*) FILTER (WHERE coalesce(s.status, 'success') = 'failure')::bigint AS failure_count,
        coalesce(sum(s.total_tokens), 0)::bigint AS total_tokens,
        coalesce(sum(s.prompt_tokens), 0)::bigint AS input_tokens,
        coalesce(sum(s.completion_tokens), 0)::bigint AS output_tokens,
        coalesce(sum({litellm_cached_tokens_sql()}), 0)::bigint AS cached_tokens,
        coalesce(sum((s.metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint), 0)::bigint AS reasoning_tokens,
        coalesce(sum(s.spend), 0)::float8 AS spend_usd
    FROM "LiteLLM_SpendLogs" s
    LEFT JOIN "LiteLLM_VerificationToken" v ON s.api_key = v.token
    GROUP BY 1, 2, 3
), user_rows AS (
    SELECT
        period,
        user_email,
        count(distinct key_label)::bigint AS num_keys,
        array_agg(distinct key_label ORDER BY key_label) AS api_keys,
        sum(total_requests)::bigint AS total_requests,
        sum(success_count)::bigint AS success_count,
        sum(failure_count)::bigint AS failure_count,
        sum(total_tokens)::bigint AS total_tokens,
        sum(input_tokens)::bigint AS input_tokens,
        sum(output_tokens)::bigint AS output_tokens,
        sum(cached_tokens)::bigint AS cached_tokens,
        sum(reasoning_tokens)::bigint AS reasoning_tokens,
        sum(spend_usd)::float8 AS spend_usd
    FROM rows
    GROUP BY 1, 2
)
SELECT coalesce(json_agg(row_to_json(user_rows) ORDER BY {order_sql}), '[]'::json) FROM user_rows;
"""
    return litellm_psql_json(sql, timeout=30) or []


def litellm_key_match_sql(api_key, alias_expr="v.key_alias", api_key_expr="s.api_key"):
    key_literal = _sql_literal(api_key)
    suffix_literal = _sql_literal(f":{api_key}")
    return (
        f"({api_key_expr} = {key_literal} OR {alias_expr} = {key_literal} "
        f"OR right(coalesce({alias_expr}, ''), {len(api_key) + 1}) = {suffix_literal})"
    )


def litellm_keys_match_sql(keys, alias_expr="v.key_alias", api_key_expr="s.api_key"):
    values = [str(key or "").strip() for key in (keys or []) if str(key or "").strip()]
    if not values:
        return ""
    key_values = ",".join(_sql_literal(key) for key in values)
    suffix_terms = " OR ".join(
        f"right(coalesce({alias_expr}, ''), {len(key) + 1}) = {_sql_literal(f':{key}')}"
        for key in values
    )
    return f"({api_key_expr} IN ({key_values}) OR {alias_expr} IN ({key_values}) OR {suffix_terms})"


def litellm_user_key_totals(email, keys=None):
    clauses = []
    identity = litellm_spend_identity_sql()
    if email:
        clauses.append(f"lower({identity}) = lower({_sql_literal(email)})")
    key_clause = litellm_keys_match_sql(keys)
    if key_clause:
        clauses.append(key_clause)
    where_sql = "WHERE " + " AND ".join(clauses) if clauses else ""
    today = _sql_literal(beijing_today())
    sql = f"""
WITH rows AS (
    SELECT
        s.api_key,
        coalesce(nullif(v.key_alias, ''), s.api_key) AS key_id,
        coalesce(nullif(v.key_alias, ''), left(s.api_key, 16)) AS key_label,
        {identity} AS user_email,
        count(*)::bigint AS total_requests,
        coalesce(sum(s.total_tokens), 0)::bigint AS total_tokens,
        coalesce(sum(s.prompt_tokens), 0)::bigint AS input_tokens,
        coalesce(sum(s.completion_tokens), 0)::bigint AS output_tokens,
        coalesce(sum({litellm_cached_tokens_sql()}), 0)::bigint AS cached_tokens,
        coalesce(sum((s.metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint), 0)::bigint AS reasoning_tokens,
        coalesce(sum(s.spend), 0)::float8 AS spend_usd,
        count(*) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours'))::bigint AS today_requests,
        coalesce(sum(s.total_tokens) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::bigint AS today_tokens,
        coalesce(sum(s.prompt_tokens) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::bigint AS today_input_tokens,
        coalesce(sum(s.completion_tokens) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::bigint AS today_output_tokens,
        coalesce(sum({litellm_cached_tokens_sql()}) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::bigint AS today_cached_tokens,
        coalesce(sum(s.spend) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::float8 AS today_spend_usd,
        coalesce(sum((s.metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::bigint AS today_reasoning_tokens
    FROM "LiteLLM_SpendLogs" s
    LEFT JOIN "LiteLLM_VerificationToken" v ON s.api_key = v.token
    {where_sql}
    GROUP BY 1, 2, 3, 4
)
SELECT coalesce(json_agg(row_to_json(rows) ORDER BY total_tokens DESC), '[]'::json) FROM rows;
"""
    return litellm_psql_json(sql, timeout=30) or []


def litellm_user_usage_summary(email, keys=None):
    email = _normalize_email(email)
    if not email:
        return {}
    identity = litellm_spend_identity_sql()
    clauses = [f"lower({identity}) = lower({_sql_literal(email)})"]
    key_values = ",".join(_sql_literal(k) for k in (keys or []) if k)
    if key_values:
        clauses.append(f"s.api_key IN ({key_values}) OR v.key_alias IN ({key_values})")
    where_sql = " OR ".join(f"({clause})" for clause in clauses)
    today = _sql_literal(beijing_today())
    sql = f"""
SELECT json_build_object(
    'today', {today},
    'total_requests', count(*)::bigint,
    'success_count', count(*) FILTER (WHERE coalesce(s.status, 'success') != 'failure')::bigint,
    'failure_count', count(*) FILTER (WHERE coalesce(s.status, 'success') = 'failure')::bigint,
    'total_tokens', coalesce(sum(s.total_tokens), 0)::bigint,
    'input_tokens', coalesce(sum(s.prompt_tokens), 0)::bigint,
    'output_tokens', coalesce(sum(s.completion_tokens), 0)::bigint,
    'cached_tokens', coalesce(sum({litellm_cached_tokens_sql()}), 0)::bigint,
    'reasoning_tokens', coalesce(sum((s.metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint), 0)::bigint,
    'spend_usd', coalesce(sum(s.spend), 0)::float8,
    'today_requests', count(*) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours'))::bigint,
    'today_success_count', count(*) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours') AND coalesce(s.status, 'success') != 'failure')::bigint,
    'today_failure_count', count(*) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours') AND coalesce(s.status, 'success') = 'failure')::bigint,
    'today_tokens', coalesce(sum(s.total_tokens) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::bigint,
    'today_input_tokens', coalesce(sum(s.prompt_tokens) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::bigint,
    'today_output_tokens', coalesce(sum(s.completion_tokens) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::bigint,
    'today_cached_tokens', coalesce(sum({litellm_cached_tokens_sql()}) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::bigint,
    'today_reasoning_tokens', coalesce(sum((s.metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::bigint,
    'today_spend_usd', coalesce(sum(s.spend) FILTER (WHERE s."endTime" >= ({today}::date::timestamp - interval '8 hours') AND s."endTime" < ({today}::date::timestamp + interval '16 hours')), 0)::float8
)
FROM "LiteLLM_SpendLogs" s
LEFT JOIN "LiteLLM_VerificationToken" v ON s.api_key = v.token
WHERE {where_sql};
"""
    return litellm_psql_json(sql, timeout=15) or {}


def attach_user_usage_breakdown(summary):
    summary = dict(summary or {})
    for key in (
        "today_requests",
        "today_success_count",
        "today_failure_count",
        "today_tokens",
        "today_input_tokens",
        "today_output_tokens",
        "today_cached_tokens",
        "today_reasoning_tokens",
        "today_spend_usd",
        "total_requests",
        "success_count",
        "failure_count",
        "total_tokens",
        "input_tokens",
        "output_tokens",
        "cached_tokens",
        "reasoning_tokens",
        "spend_usd",
    ):
        summary[key] = _float_usage_value(summary.get(key)) if key.endswith("_usd") or key == "spend_usd" else _int_usage_value(summary.get(key))
    summary["today"] = summary.get("today") or beijing_today()
    today_breakdown = build_litellm_token_breakdown(
        summary.get("today_tokens", 0),
        summary.get("today_input_tokens", 0),
        summary.get("today_output_tokens", 0),
        summary.get("today_cached_tokens", 0),
        summary.get("today_reasoning_tokens", 0),
        summary.get("today_spend_usd", 0),
    )
    total_breakdown = build_litellm_token_breakdown(
        summary.get("total_tokens", 0),
        summary.get("input_tokens", 0),
        summary.get("output_tokens", 0),
        summary.get("cached_tokens", 0),
        summary.get("reasoning_tokens", 0),
        summary.get("spend_usd", 0),
    )
    summary["token_breakdown"] = {"today": today_breakdown, "total": total_breakdown}
    summary["today_estimated_cost_usd"] = today_breakdown["cost_usd"]
    summary["estimated_cost_usd"] = total_breakdown["cost_usd"]
    summary["failed_requests"] = _int_usage_value(summary.get("failure_count"))
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()
    return summary


def get_user_usage_summary_cached(email, keys=None):
    email = _normalize_email(email)
    key_hash = hashlib.sha1("\0".join(sorted(keys or [])).encode("utf-8")).hexdigest()
    cache_key = f"{email}:{key_hash}"
    now = time.time()
    with _user_usage_summary_cache_lock:
        cached = _user_usage_summary_cache["data"].get(cache_key)
        if cached and (now - cached["last_update"]) < _user_usage_summary_cache["ttl"]:
            return {**cached["data"], "cache_age_seconds": round(now - cached["last_update"], 3)}

    summary = attach_user_usage_breakdown(litellm_user_usage_summary(email, keys))
    summary["email"] = email
    summary["cache_status"] = "postgres"
    with _user_usage_summary_cache_lock:
        _user_usage_summary_cache["data"][cache_key] = {
            "data": summary,
            "last_update": time.time(),
        }
    return {**summary, "cache_age_seconds": 0}


def mask_speed_identity(email):
    prefix = str(email or "").split("@", 1)[0].strip()
    segment = prefix.split(".", 1)[0].strip() or prefix
    source = re.sub(r"[^A-Za-z0-9_-]+", "", segment) or "?"
    return source[:10].upper()


def speed_level_for(rate, metric):
    if rate <= 0:
        return {"label": "熄火", "index": 0, "progress": 0}
    request_thresholds = [
        ("步行", 0),
        ("跑步", 0.2),
        ("自行车", 0.6),
        ("电动车", 1.5),
        ("汽车", 3),
        ("直升机", 8),
        ("飞机", 18),
        ("火箭", 40),
        ("UFO", 80),
    ]
    token_thresholds = [(label, int(threshold * 100000)) for label, threshold in request_thresholds]
    thresholds = token_thresholds if metric == "tokens" else request_thresholds
    level_index = 0
    for idx, (_, threshold) in enumerate(thresholds):
        if rate >= threshold:
            level_index = idx
    next_threshold = thresholds[min(level_index + 1, len(thresholds) - 1)][1]
    current_threshold = thresholds[level_index][1]
    if level_index >= len(thresholds) - 1:
        progress = 100
    elif next_threshold <= current_threshold:
        progress = 0
    else:
        progress = max(0, min(100, int((rate - current_threshold) * 100 / (next_threshold - current_threshold))))
    return {"label": thresholds[level_index][0], "index": level_index, "progress": progress}


def litellm_realtime_speed_leaderboard(email, window_seconds=60, limit=8):
    email = _normalize_email(email)
    window_seconds = max(30, min(int(window_seconds or 60), 600))
    limit = max(3, min(int(limit or 8), 20))
    identity = litellm_spend_identity_sql()
    active_seconds = max(15, min(window_seconds // 3, 30))
    sql = f"""
WITH rows AS (
    SELECT
        lower({identity}) AS email,
        count(*)::bigint AS requests,
        coalesce(sum(s.total_tokens), 0)::bigint AS tokens,
        count(*) FILTER (WHERE s."endTime" >= now() - ({active_seconds} * interval '1 second'))::bigint AS active_requests,
        coalesce(sum(s.total_tokens) FILTER (WHERE s."endTime" >= now() - ({active_seconds} * interval '1 second')), 0)::bigint AS active_tokens,
        max(s."endTime") AS last_seen
    FROM "LiteLLM_SpendLogs" s
    LEFT JOIN "LiteLLM_VerificationToken" v ON s.api_key = v.token
    WHERE s."endTime" >= now() - ({window_seconds} * interval '1 second')
    GROUP BY 1
), ranked AS (
    SELECT
        email,
        requests,
        tokens,
        active_requests,
        active_tokens,
        last_seen,
        row_number() OVER (ORDER BY active_tokens DESC, active_requests DESC, tokens DESC, email ASC) AS token_rank,
        row_number() OVER (ORDER BY active_requests DESC, active_tokens DESC, requests DESC, email ASC) AS request_rank
    FROM rows
    WHERE email IS NOT NULL AND email != '' AND email != 'unknown'
)
SELECT coalesce(json_agg(row_to_json(ranked) ORDER BY token_rank), '[]'::json) FROM ranked;
"""
    rows = litellm_psql_json(sql, timeout=10) or []
    built = []
    current = None
    for row in rows:
        row_email = _normalize_email(row.get("email"))
        tokens = _int_usage_value(row.get("tokens"))
        requests = _int_usage_value(row.get("requests"))
        active_tokens = _int_usage_value(row.get("active_tokens"))
        active_requests = _int_usage_value(row.get("active_requests"))
        item = {
            "email_mask": mask_speed_identity(row_email),
            "is_current_user": row_email == email,
            "tokens": tokens,
            "requests": requests,
            "active_tokens": active_tokens,
            "active_requests": active_requests,
            "tokens_per_minute": round(active_tokens * 60 / active_seconds, 2),
            "requests_per_minute": round(active_requests * 60 / active_seconds, 2),
            "token_rank": _int_usage_value(row.get("token_rank")),
            "request_rank": _int_usage_value(row.get("request_rank")),
            "last_seen": row.get("last_seen"),
        }
        item["token_level"] = speed_level_for(item["tokens_per_minute"], "tokens")
        item["request_level"] = speed_level_for(item["requests_per_minute"], "requests")
        built.append(item)
        if item["is_current_user"]:
            current = item
    if email and not current:
        current = {
            "email_mask": mask_speed_identity(email),
            "is_current_user": True,
            "tokens": 0,
            "requests": 0,
            "tokens_per_minute": 0,
            "requests_per_minute": 0,
            "token_rank": None,
            "request_rank": None,
            "last_seen": None,
            "token_level": speed_level_for(0, "tokens"),
            "request_level": speed_level_for(0, "requests"),
        }
    return {
        "window_seconds": window_seconds,
        "window_minutes": round(window_seconds / 60, 2),
        "active_seconds": active_seconds,
        "top": built[:limit],
        "current_user": current,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


def litellm_key_timeseries(api_key, date_from, date_to, email="", hourly=False):
    key_match = litellm_key_match_sql(api_key)
    identity = litellm_spend_identity_sql()
    email_clause = f"AND lower({identity}) = lower({_sql_literal(email)})" if email else ""
    if hourly:
        sql = f"""
WITH hours AS (
    SELECT generate_series({_sql_literal(date_from)}::date, {_sql_literal(date_from)}::date + interval '23 hours', interval '1 hour') AS hour_start
), matched AS (
    SELECT
        s."startTime",
        s."endTime",
        s.status,
        s.total_tokens,
        s.prompt_tokens,
        s.completion_tokens,
        s.metadata,
        s.spend
    FROM "LiteLLM_SpendLogs" s
    LEFT JOIN "LiteLLM_VerificationToken" v ON s.api_key = v.token
    WHERE {key_match}
      {email_clause}
), rows AS (
    SELECT
        hours.hour_start::date::text AS date,
        to_char(hours.hour_start, 'HH24') AS hour,
        coalesce(count(s.*), 0)::bigint AS requests,
        coalesce(count(s.*) FILTER (WHERE coalesce(s.status, 'success') != 'failure'), 0)::bigint AS success_count,
        coalesce(count(s.*) FILTER (WHERE coalesce(s.status, 'success') = 'failure'), 0)::bigint AS failure_count,
        coalesce(sum(s.total_tokens), 0)::bigint AS total_tokens,
        coalesce(sum(s.prompt_tokens), 0)::bigint AS input_tokens,
        coalesce(sum(s.completion_tokens), 0)::bigint AS output_tokens,
        coalesce(sum({litellm_cached_tokens_sql()}), 0)::bigint AS cached_tokens,
        coalesce(sum((s.metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint), 0)::bigint AS reasoning_tokens,
        coalesce(sum(s.spend), 0)::float8 AS spend_usd,
        coalesce(avg(extract(epoch FROM (s."endTime" - s."startTime")) * 1000), 0)::float8 AS avg_latency_ms
    FROM hours
    LEFT JOIN matched s
      ON s."endTime" >= hours.hour_start - interval '8 hours'
     AND s."endTime" < hours.hour_start + interval '1 hour' - interval '8 hours'
    GROUP BY hours.hour_start
    ORDER BY hours.hour_start
)
SELECT coalesce(json_agg(row_to_json(rows)), '[]'::json) FROM rows;
"""
        return litellm_psql_json(sql, timeout=30) or []
    sql = f"""
WITH days AS (
    SELECT generate_series({_sql_literal(date_from)}::date, {_sql_literal(date_to)}::date, interval '1 day')::date AS day
), matched AS (
    SELECT
        s."startTime",
        s."endTime",
        s.status,
        s.total_tokens,
        s.prompt_tokens,
        s.completion_tokens,
        s.metadata,
        s.spend
    FROM "LiteLLM_SpendLogs" s
    LEFT JOIN "LiteLLM_VerificationToken" v ON s.api_key = v.token
    WHERE {key_match}
      {email_clause}
), rows AS (
    SELECT
        days.day::text AS date,
        coalesce(count(s.*), 0)::bigint AS requests,
        coalesce(count(s.*) FILTER (WHERE coalesce(s.status, 'success') != 'failure'), 0)::bigint AS success_count,
        coalesce(count(s.*) FILTER (WHERE coalesce(s.status, 'success') = 'failure'), 0)::bigint AS failure_count,
        coalesce(sum(s.total_tokens), 0)::bigint AS total_tokens,
        coalesce(sum(s.prompt_tokens), 0)::bigint AS input_tokens,
        coalesce(sum(s.completion_tokens), 0)::bigint AS output_tokens,
        coalesce(sum({litellm_cached_tokens_sql()}), 0)::bigint AS cached_tokens,
        coalesce(sum((s.metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint), 0)::bigint AS reasoning_tokens,
        coalesce(sum(s.spend), 0)::float8 AS spend_usd,
        coalesce(avg(extract(epoch FROM (s."endTime" - s."startTime")) * 1000), 0)::float8 AS avg_latency_ms
    FROM days
    LEFT JOIN matched s
      ON s."endTime" >= days.day::timestamp - interval '8 hours'
     AND s."endTime" < days.day::timestamp + interval '16 hours'
    GROUP BY days.day
    ORDER BY days.day
)
SELECT coalesce(json_agg(row_to_json(rows)), '[]'::json) FROM rows;
"""
    return litellm_psql_json(sql, timeout=30) or []


def litellm_recent_requests(api_key="", email="", hours=24, limit=100):
    clauses = []
    identity = litellm_spend_identity_sql()
    if api_key:
        key_literal = _sql_literal(api_key)
        clauses.append(f"(s.api_key = {key_literal} OR v.key_alias = {key_literal})")
    if email:
        clauses.append(f"lower({identity}) = lower({_sql_literal(email)})")
    where_sql = " AND ".join(clauses) if clauses else "true"
    hours = max(1, min(int(hours or 24), 168))
    limit = max(1, min(int(limit or 100), 500))
    sql = f"""
WITH rows AS (
    SELECT
        s."startTime" AS start_time,
        s."endTime" AS end_time,
        coalesce(nullif(v.key_alias, ''), left(s.api_key, 16)) AS key_label,
        {identity} AS user_email,
        s.model,
        coalesce(s.status, 'success') AS status,
        coalesce(s.total_tokens, 0)::bigint AS total_tokens,
        coalesce(s.prompt_tokens, 0)::bigint AS input_tokens,
        coalesce(s.completion_tokens, 0)::bigint AS output_tokens,
        coalesce({litellm_cached_tokens_sql()}, 0)::bigint AS cached_tokens,
        coalesce((s.metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint, 0)::bigint AS reasoning_tokens,
        coalesce(s.spend, 0)::float8 AS spend_usd,
        coalesce(extract(epoch FROM (s."endTime" - s."startTime")) * 1000, 0)::float8 AS latency_ms,
        s.metadata->>'call_type' AS call_type,
        s.metadata->>'request_id' AS request_id,
        s.metadata->>'user_api_base' AS user_api_base
    FROM "LiteLLM_SpendLogs" s
    LEFT JOIN "LiteLLM_VerificationToken" v ON s.api_key = v.token
    WHERE s."endTime" >= now() - interval '{hours} hours'
      AND {where_sql}
    ORDER BY s."endTime" DESC
    LIMIT {limit}
)
SELECT coalesce(json_agg(row_to_json(rows)), '[]'::json) FROM rows;
"""
    return litellm_psql_json(sql, timeout=30) or []


def monitor_log_recent_entries(api_key, hours=24, limit=500, log_dir=None):
    api_key = str(api_key or "").strip()
    if not api_key:
        return []
    hours = max(1, min(int(hours or 24), 168))
    limit = max(1, min(int(limit or 500), 5000))
    cutoff = datetime.utcnow() - timedelta(hours=hours)
    root = os.path.abspath(log_dir or MONITOR_LOG_DIR)
    if not os.path.isdir(root):
        return []

    entries = []
    for name in sorted(os.listdir(root), reverse=True):
        if not name.endswith(".log"):
            continue
        path = os.path.abspath(os.path.join(root, name))
        if not path.startswith(root + os.sep):
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as handle:
                current = None
                in_body = False
                for raw_line in handle:
                    line = raw_line.rstrip("\n")
                    if line.startswith("========== ") and line.endswith(" ==========") and "END" not in line:
                        if current and current.get("key") == api_key:
                            entries.append(current)
                        timestamp = line.removeprefix("========== ").removesuffix(" ==========").strip()
                        current = {"timestamp": timestamp, "file": name}
                        in_body = False
                        continue
                    if current is None:
                        continue
                    if line.startswith("--- REQUEST BODY ---") or line.startswith("--- RESPONSE ---"):
                        in_body = True
                        continue
                    if line.startswith("========== END =========="):
                        if current.get("key") == api_key:
                            entries.append(current)
                        current = None
                        in_body = False
                        if len(entries) >= limit:
                            break
                        continue
                    if in_body or ": " not in line:
                        continue
                    field, value = line.split(": ", 1)
                    field_key = field.strip().lower().replace("-", "_")
                    if field_key in {"key", "method", "url", "remote", "status", "request_size", "response_size", "user_agent", "content_type", "originator"}:
                        current[field_key] = value.strip()
                if current and current.get("key") == api_key:
                    entries.append(current)
        except OSError:
            continue
        filtered = []
        for entry in entries:
            try:
                ts = datetime.strptime(str(entry.get("timestamp") or ""), "%Y-%m-%dT%H:%M:%SZ")
            except ValueError:
                continue
            if ts >= cutoff:
                safe = dict(entry)
                safe["key"] = mask_api_key(safe.get("key", ""))
                filtered.append(safe)
        entries = filtered[:limit]
        if len(entries) >= limit:
            break
    return entries[:limit]


def litellm_usage_rows_for_dates(dates):
    dates = sorted({str(date) for date in dates if date})
    if not dates:
        return []
    date_values = ",".join(f"('{date.replace(chr(39), chr(39) + chr(39))}')" for date in dates)
    sql = f"""
WITH days(day) AS (
    VALUES {date_values}
), bounds AS (
    SELECT
        day::date AS day,
        (day::date::timestamp - interval '8 hours') AS start_utc,
        (day::date::timestamp + interval '16 hours') AS end_utc
    FROM days
), rows AS (
    SELECT
        bounds.day::text AS date,
        coalesce(nullif(v.metadata->>'email', ''), nullif(v.user_id, ''), nullif(s.user, ''), 'unknown') AS user_email,
        concat('litellm:', coalesce(nullif(v.key_alias, ''), left(s.api_key, 16))) AS api_key,
        count(*)::bigint AS total_requests,
        count(*) FILTER (WHERE coalesce(s.status, 'success') != 'failure')::bigint AS success_count,
        count(*) FILTER (WHERE coalesce(s.status, 'success') = 'failure')::bigint AS failure_count,
        coalesce(sum(s.total_tokens), 0)::bigint AS total_tokens,
        coalesce(sum(s.prompt_tokens), 0)::bigint AS input_tokens,
        coalesce(sum(s.completion_tokens), 0)::bigint AS output_tokens,
        0::bigint AS cached_tokens,
        0::bigint AS reasoning_tokens
    FROM bounds
    JOIN "LiteLLM_SpendLogs" s
      ON s."endTime" >= bounds.start_utc
     AND s."endTime" < bounds.end_utc
    LEFT JOIN "LiteLLM_VerificationToken" v
      ON s.api_key = v.token
    GROUP BY 1, 2, 3
)
SELECT coalesce(json_agg(row_to_json(rows) ORDER BY date, total_tokens DESC), '[]'::json)
FROM rows;
"""
    data = litellm_psql_json(sql, timeout=30)
    return data or []


def sync_litellm_usage_to_database(dates):
    rows = litellm_usage_rows_for_dates(dates)
    stats = db.replace_litellm_usage_for_dates(rows)
    rebuild_stats = db.rebuild_daily_usage_from_user_usage(dates)
    clear_persistent_floor_cache()
    with _litellm_usage_cache_lock:
        _litellm_usage_cache["data"] = None
        _litellm_usage_cache["last_update"] = 0
    return {
        **stats,
        "rebuilt_dates": rebuild_stats.get("dates", 0),
        "tokens": sum(_int_usage_value(row.get("total_tokens")) for row in rows),
        "requests": sum(_int_usage_value(row.get("total_requests")) for row in rows),
    }


def query_litellm_spendlogs_aggregate():
    database_url = litellm_database_url()
    if not database_url:
        return None

    now = time.time()
    with _litellm_usage_cache_lock:
        cached = _litellm_usage_cache["data"]
        if cached and (now - _litellm_usage_cache["last_update"]) < _litellm_usage_cache["ttl"]:
            return dict(cached)

    today = beijing_today()
    today_sql = today.replace("'", "''")
    cached_sql = litellm_cached_tokens_sql()
    sql = f"""
WITH bounds AS (
    SELECT
        ('{today_sql}'::date::timestamp - interval '8 hours') AS today_start_utc,
        ('{today_sql}'::date::timestamp + interval '16 hours') AS tomorrow_start_utc
), totals AS (
    SELECT
        count(*)::bigint AS total_requests,
        count(*) FILTER (WHERE coalesce(status, 'success') != 'failure')::bigint AS success_count,
        count(*) FILTER (WHERE coalesce(status, 'success') = 'failure')::bigint AS failure_count,
        coalesce(sum(total_tokens), 0)::bigint AS total_tokens,
        coalesce(sum(prompt_tokens), 0)::bigint AS input_tokens,
        coalesce(sum(completion_tokens), 0)::bigint AS output_tokens,
        coalesce(sum({cached_sql}), 0)::bigint AS cached_tokens,
        coalesce(sum((metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint), 0)::bigint AS reasoning_tokens,
        coalesce(sum(spend), 0)::float8 AS spend_usd,
        max("endTime") AS last_end_time
    FROM "LiteLLM_SpendLogs" s
), today_totals AS (
    SELECT
        count(*)::bigint AS today_requests,
        count(*) FILTER (WHERE coalesce(status, 'success') != 'failure')::bigint AS today_success_count,
        count(*) FILTER (WHERE coalesce(status, 'success') = 'failure')::bigint AS today_failure_count,
        coalesce(sum(total_tokens), 0)::bigint AS today_tokens,
        coalesce(sum(prompt_tokens), 0)::bigint AS today_input_tokens,
        coalesce(sum(completion_tokens), 0)::bigint AS today_output_tokens,
        coalesce(sum({cached_sql}), 0)::bigint AS today_cached_tokens,
        coalesce(sum((metadata->'usage_object'->'completion_tokens_details'->>'reasoning_tokens')::bigint), 0)::bigint AS today_reasoning_tokens,
        coalesce(sum(spend), 0)::float8 AS today_spend_usd,
        max("endTime") AS today_last_end_time
    FROM "LiteLLM_SpendLogs" s, bounds
    WHERE "endTime" >= bounds.today_start_utc
      AND "endTime" < bounds.tomorrow_start_utc
)
SELECT json_build_object(
    'today', '{today_sql}',
    'today_requests', today_totals.today_requests,
    'today_success_count', today_totals.today_success_count,
    'today_failure_count', today_totals.today_failure_count,
    'today_tokens', today_totals.today_tokens,
    'today_input_tokens', today_totals.today_input_tokens,
    'today_output_tokens', today_totals.today_output_tokens,
    'today_cached_tokens', today_totals.today_cached_tokens,
    'today_reasoning_tokens', today_totals.today_reasoning_tokens,
    'today_spend_usd', today_totals.today_spend_usd,
    'today_last_end_time', today_totals.today_last_end_time,
    'total_requests', totals.total_requests,
    'success_count', totals.success_count,
    'failure_count', totals.failure_count,
    'total_tokens', totals.total_tokens,
    'input_tokens', totals.input_tokens,
    'output_tokens', totals.output_tokens,
    'cached_tokens', totals.cached_tokens,
    'reasoning_tokens', totals.reasoning_tokens,
    'spend_usd', totals.spend_usd,
    'last_end_time', totals.last_end_time
)
FROM totals, today_totals;
"""
    try:
        payload = litellm_psql_json(sql, timeout=5)
        if not payload:
            print("[UsageSummary] LiteLLM aggregate query failed")
            return None
        aggregate = {
            "today": today,
            "today_requests": _int_usage_value(payload.get("today_requests")),
            "today_success_count": _int_usage_value(payload.get("today_success_count")),
            "today_failure_count": _int_usage_value(payload.get("today_failure_count")),
            "today_tokens": _int_usage_value(payload.get("today_tokens")),
            "today_input_tokens": _int_usage_value(payload.get("today_input_tokens")),
            "today_output_tokens": _int_usage_value(payload.get("today_output_tokens")),
            "today_cached_tokens": _int_usage_value(payload.get("today_cached_tokens")),
            "today_reasoning_tokens": _int_usage_value(payload.get("today_reasoning_tokens")),
            "today_spend_usd": round(_float_usage_value(payload.get("today_spend_usd")), 6),
            "today_last_end_time": payload.get("today_last_end_time"),
            "total_requests": _int_usage_value(payload.get("total_requests")),
            "success_count": _int_usage_value(payload.get("success_count")),
            "failure_count": _int_usage_value(payload.get("failure_count")),
            "total_tokens": _int_usage_value(payload.get("total_tokens")),
            "input_tokens": _int_usage_value(payload.get("input_tokens")),
            "output_tokens": _int_usage_value(payload.get("output_tokens")),
            "cached_tokens": _int_usage_value(payload.get("cached_tokens")),
            "reasoning_tokens": _int_usage_value(payload.get("reasoning_tokens")),
            "spend_usd": round(_float_usage_value(payload.get("spend_usd")), 6),
            "last_end_time": payload.get("last_end_time"),
        }
        with _litellm_usage_cache_lock:
            _litellm_usage_cache["data"] = dict(aggregate)
            _litellm_usage_cache["last_update"] = time.time()
        return aggregate
    except Exception as e:
        print(f"[UsageSummary] LiteLLM aggregate unavailable: {e}")
        return None


def persisted_usage_summary():
    summary = persistent_usage_summary_floor() or {
        "today": beijing_today(),
        "today_tokens": 0,
        "today_requests": 0,
        "total_tokens": 0,
        "total_requests": 0,
        "success_count": 0,
        "failure_count": 0,
        "failed_requests": 0,
    }
    summary = dict(summary)
    summary["summary_sources"] = ["key_portal_sqlite"]

    litellm = query_litellm_spendlogs_aggregate()
    if litellm:
        summary["litellm_included"] = True
        summary["litellm_usage"] = litellm
        summary["summary_sources"].append("litellm_spendlogs")
        if litellm.get("today") == summary.get("today"):
            summary["today_tokens"] = max(_int_usage_value(summary.get("today_tokens")), litellm["today_tokens"])
            summary["today_requests"] = max(_int_usage_value(summary.get("today_requests")), litellm["today_requests"])
        summary["total_tokens"] = max(_int_usage_value(summary.get("total_tokens")), litellm["total_tokens"])
        summary["total_requests"] = max(_int_usage_value(summary.get("total_requests")), litellm["total_requests"])
    else:
        summary["litellm_included"] = False

    summary["failed_requests"] = _int_usage_value(summary.get("failure_count"))
    return summary


def attach_live_node_metadata(summary, cluster_summary):
    summary["cluster_partial"] = bool((cluster_summary or {}).get("node_errors"))
    summary["node_errors"] = (cluster_summary or {}).get("node_errors", [])
    summary["nodes"] = (cluster_summary or {}).get("nodes", [])
    summary["summary_sources"].append("cliproxy_usage_summary_nodes")
    live_summary = build_usage_summary_response(cluster_summary or {})
    summary["live_usage_summary"] = {
        "today_tokens": live_summary.get("today_tokens", 0),
        "today_requests": live_summary.get("today_requests", 0),
        "total_tokens": live_summary.get("total_tokens", 0),
        "total_requests": live_summary.get("total_requests", 0),
    }
    return summary


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
    redis_cached = portal_state.cache_get_json("usage_summary")
    if redis_cached:
        with _usage_summary_cache_lock:
            _usage_summary_cache["data"] = redis_cached
            _usage_summary_cache["last_update"] = now
        return dict(redis_cached, cache_age_seconds=0, cache_status="redis"), None

    litellm = query_litellm_spendlogs_aggregate()
    if litellm:
        summary = {
            **litellm,
            "failed_requests": _int_usage_value(litellm.get("failure_count")),
            "summary_sources": ["litellm_spendlogs"],
            "litellm_included": True,
            "litellm_usage": litellm,
            "cluster_partial": False,
            "node_errors": [],
            "nodes": [],
            "live_usage_summary": {},
        }
    else:
        cluster_summary = get_cluster_usage_summary()
        summary = build_usage_summary_response(cluster_summary)
        summary = apply_persistent_usage_floor(summary)
        summary["summary_sources"] = ["key_portal_sqlite_fallback"]
        summary["litellm_included"] = False
        summary = attach_live_node_metadata(summary, cluster_summary)

    summary = attach_token_breakdown(summary)
    summary["generated_at"] = datetime.now(timezone.utc).isoformat()

    now = time.time()
    with _usage_summary_cache_lock:
        _usage_summary_cache["data"] = summary
        _usage_summary_cache["last_update"] = now
    portal_state.cache_set_json("usage_summary", summary, max(2, int(_usage_summary_cache["ttl"] * 2)))
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
    return parsed.strftime("%Y-%m-%d"), parsed.hour


def beijing_now():
    return datetime.now(BEIJING_TZ).replace(tzinfo=None)


def beijing_today():
    return beijing_now().strftime("%Y-%m-%d")


status_service = status_events.StatusEventsService(
    config=config,
    portal_state=portal_state,
    feishu=feishu,
    database=db,
    nodes=CLIPROXY_NODES,
    beijing_today=beijing_today,
    int_usage_value=_int_usage_value,
    sql_literal=_sql_literal,
    litellm_psql_json=litellm_psql_json,
    usage_summary_loader=lambda: get_usage_summary_cached(),
)

auth_stats = auth_stats_service.AuthStatsService(
    portal_state=portal_state,
    nodes=CLIPROXY_NODES,
    call_management_api_node=call_management_api_node,
    get_cluster_usage=get_cluster_usage,
    get_cluster_auth_files=get_cluster_auth_files,
    usage_summary_loader=lambda: get_usage_summary_cached(),
    parse_detail_time=parse_detail_time,
    parse_detail_time_utc=parse_detail_time_utc,
    build_token_breakdown=build_token_breakdown,
)


def get_auth_stats_cached():
    return auth_stats.get_cached()


def clear_auth_stats_cache():
    auth_stats.clear_cache()


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
    end_hour = beijing_now().replace(minute=0, second=0, microsecond=0)
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
            daily["total_tokens"] = int(tokens_by_day.get(today, 0) or 0)
            daily["total_requests"] = int(requests_by_day.get(today, 0) or 0)
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
        hour_key = bucket["key"][-2:]
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
                hour_value = (parsed + timedelta(hours=8)).replace(minute=0, second=0, microsecond=0)
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


def get_auth_files():
    """Get list of auth files from all CLIProxyAPI nodes."""
    files, _ = get_cluster_auth_files()
    return files


def get_auth_file_detail(path):
    """Get auth file detail including expiry time."""
    name = os.path.basename(path or "")
    if not name:
        return None
    data, err = call_management_api("GET", f"/v0/management/auth-files/download?name={quote(name)}")
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
    """Refresh reporting caches without pulling full CLIProxyAPI usage payloads."""
    try:
        with _litellm_usage_cache_lock:
            _litellm_usage_cache["data"] = None
            _litellm_usage_cache["last_update"] = 0
        with _recent_hours_cache_lock:
            _recent_hours_cache["data"] = None
            _recent_hours_cache["last_update"] = 0
            _recent_hours_cache["refreshing"] = False
        with _usage_history_response_cache_lock:
            _usage_history_response_cache["data"] = None
            _usage_history_response_cache["last_update"] = 0
            _usage_history_response_cache["refreshing"] = False
        query_litellm_spendlogs_aggregate()
        litellm_usage_history_aggregated(days=2)
        print("[UsageSync] Refreshed LiteLLM SpendLogs reporting caches")
        return True
    except Exception as e:
        print(f"[UsageSync] LiteLLM cache refresh error: {e}")
        import traceback
        traceback.print_exc()
        return False


def get_usage_history_aggregated():
    """Get usage history with daily, monthly, and yearly aggregations."""
    data = litellm_usage_history_aggregated()
    if data:
        data["source"] = "litellm_spendlogs"
        return data
    data = db.get_usage_aggregated()
    data["source"] = "key_portal_sqlite_fallback"
    return data


def enrich_usage_breakdowns(data):
    for row in data.get("history", []) or []:
        spend_usd = _float_usage_value(row.get("spend_usd"))
        breakdown = build_litellm_token_breakdown(row.get("total_tokens", 0), row.get("input_tokens", 0), row.get("output_tokens", 0), row.get("cached_tokens", 0), row.get("reasoning_tokens", 0), spend_usd)
        row["token_breakdown"] = breakdown
        row["estimated_cost_usd"] = breakdown["cost_usd"]
        row["spend_usd"] = round(spend_usd, 6)
    for bucket_name in ("by_month", "by_year"):
        for bucket in (data.get(bucket_name, {}) or {}).values():
            spend_usd = _float_usage_value(bucket.get("spend_usd"))
            breakdown = build_litellm_token_breakdown(bucket.get("total_tokens", 0), bucket.get("input_tokens", 0), bucket.get("output_tokens", 0), bucket.get("cached_tokens", 0), bucket.get("reasoning_tokens", 0), spend_usd)
            bucket["token_breakdown"] = breakdown
            bucket["estimated_cost_usd"] = breakdown["cost_usd"]
            bucket["spend_usd"] = round(spend_usd, 6)
    data["token_pricing"] = litellm_spend_pricing_metadata()
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
            "cached_tokens": 0,
            "reasoning_tokens": 0,
        }
        history.append(row)
        history.sort(key=lambda item: item.get("date", ""))

    old_tokens = int(row.get("total_tokens", 0) or 0)
    old_requests = int(row.get("total_requests", 0) or 0)
    old_success = int(row.get("success_count", 0) or 0)
    old_failure = int(row.get("failure_count", 0) or 0)

    row["total_tokens"] = live_tokens
    row["total_requests"] = live_requests

    success_by_hour = usage.get("success_by_hour", {}) or {}
    failure_by_hour = usage.get("failure_by_hour", {}) or {}
    live_success = sum(int(value or 0) for value in success_by_hour.values())
    live_failure = sum(int(value or 0) for value in failure_by_hour.values())
    if live_success + live_failure == live_requests:
        row["success_count"] = live_success
        row["failure_count"] = live_failure

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
    """Deprecated standalone registration page."""
    return redirect("/")


@app.route("/my-keys")
def my_keys_page():
    """User's keys management page."""
    service_info = dict(config.SERVICE_INFO)
    service_info["base_url"] = _api_base_url()
    return render_template("my_keys.html", service_info=service_info)


@app.route("/guide")
def guide_page():
    """Beginner guide for Key Portal users."""
    service_info = dict(config.SERVICE_INFO)
    service_info["base_url"] = _api_base_url()
    return render_template(
        "guide.html",
        service_info=service_info,
        guide=portal_docs.guide_context(service_info["base_url"]),
    )


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
    """Feishu login page for Key Portal."""
    if current_portal_session():
        return redirect(_safe_next_url(request.args.get("next"), "/"))
    return render_template(
        "portal_login.html",
        next_url=_safe_next_url(request.args.get("next"), "/"),
        login_error=request.args.get("login_error", ""),
    )


@app.route("/contribute")
def contribute_page():
    """Deprecated standalone key contribution page."""
    return redirect("/")


@app.route("/status")
def status():
    """Service status and incident announcement page."""
    return render_template("status.html")


@app.route("/api/status")
def api_status():
    """Return current service status, active incidents, and recent announcements."""
    return jsonify(status_service.build_payload(include_private=is_current_admin()))


@app.route("/api/status/events/<int:event_id>", methods=["PATCH"])
def api_update_status_event(event_id):
    """Update admin notes for one status event."""
    if not is_current_admin():
        return jsonify({"error": "需要管理员权限"}), 403
    body = request.get_json(silent=True) or {}
    event = portal_state.update_status_event_note(event_id, body.get("admin_note", ""))
    if not event:
        return jsonify({"error": "事件不存在"}), 404
    return jsonify({"event": status_service.event_public(event, include_private=True)})


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


@app.route("/api/my-usage-summary")
def get_my_usage_summary():
    """Get lightweight usage counters for the current Feishu user."""
    session_data = current_portal_user()
    if not session_data:
        return jsonify({"error": "未登录"}), 401
    email = _normalize_email(session_data.get("email"))
    user_data = load_user_keys()
    user = user_data.get("users", {}).get(email, {})
    keys = list(user.get("api_keys", []) or [])
    return jsonify(get_user_usage_summary_cached(email, keys))


@app.route("/api/realtime-speed-leaderboard")
def get_realtime_speed_leaderboard():
    session_data = current_portal_user()
    if not session_data:
        return jsonify({"error": "未登录"}), 401
    email = _normalize_email(session_data.get("email"))
    try:
        window_seconds = int(request.args.get("window_seconds") or 60)
    except Exception:
        window_seconds = 60
    return jsonify(litellm_realtime_speed_leaderboard(email, window_seconds=window_seconds, limit=8))


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


def build_usage_history_response():
    data = get_usage_history_aggregated()
    enrich_usage_breakdowns(data)

    recent_hours = litellm_recent_hours(48)
    data["tokens_by_hour"] = {row.get("utcKey", row.get("hour", "")): row.get("tokens", row.get("total_tokens", 0)) for row in recent_hours}
    data["requests_by_hour"] = {row.get("utcKey", row.get("hour", "")): row.get("requests", row.get("total_requests", 0)) for row in recent_hours}
    data["success_by_hour"] = {row.get("utcKey", row.get("hour", "")): row.get("success_count", 0) for row in recent_hours}
    data["failure_by_hour"] = {row.get("utcKey", row.get("hour", "")): row.get("failure_count", 0) for row in recent_hours}
    data["avg_latency_ms_by_hour"] = {row.get("utcKey", row.get("hour", "")): row.get("avg_latency_ms") for row in recent_hours}
    data["recent_hours"] = recent_hours
    data["model_groups"] = litellm_usage_by_model_group(7)
    data["recent_hours_refreshing"] = False
    data["generated_at"] = beijing_now().isoformat()
    return data


def usage_history_empty_response(refreshing=False):
    now = beijing_now()
    return {
        "history": [],
        "by_month": {},
        "by_year": {},
        "tokens_by_hour": {},
        "requests_by_hour": {},
        "success_by_hour": {},
        "failure_by_hour": {},
        "avg_latency_ms_by_hour": {},
        "recent_hours": [],
        "model_groups": [],
        "recent_hours_refreshing": bool(refreshing),
        "generated_at": now.isoformat(),
        "refreshing": bool(refreshing),
        "cache_status": "warming",
    }


def usage_history_cache_usable(data):
    if not isinstance(data, dict):
        return False
    if data.get("cache_status") == "warming":
        return False
    if data.get("refreshing") and not (
        data.get("history") or data.get("by_month") or data.get("by_year") or data.get("recent_hours") or data.get("model_groups")
    ):
        return False
    return True


def load_usage_history_response_cache_from_disk():
    try:
        redis_cached = portal_state.cache_get_json("usage_history_response")
        if usage_history_cache_usable(redis_cached):
            return redis_cached, time.time()
        if not os.path.exists(USAGE_HISTORY_RESPONSE_CACHE_FILE):
            return None, 0
        with open(USAGE_HISTORY_RESPONSE_CACHE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not usage_history_cache_usable(data):
            return None, 0
        return data, os.path.getmtime(USAGE_HISTORY_RESPONSE_CACHE_FILE)
    except Exception as e:
        print(f"[UsageHistory] Failed to read disk cache: {e}")
        return None, 0


def save_usage_history_response_cache_to_disk(data):
    try:
        portal_state.cache_set_json("usage_history_response", data, _usage_history_response_cache["ttl"] * 5)
        tmp_path = USAGE_HISTORY_RESPONSE_CACHE_FILE + ".tmp"
        with open(tmp_path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp_path, USAGE_HISTORY_RESPONSE_CACHE_FILE)
    except Exception as e:
        print(f"[UsageHistory] Failed to write disk cache: {e}")


def refresh_usage_history_response_cache():
    try:
        data = build_usage_history_response()
        save_usage_history_response_cache_to_disk(data)
        with _usage_history_response_cache_lock:
            _usage_history_response_cache["data"] = data
            _usage_history_response_cache["last_update"] = time.time()
    finally:
        with _usage_history_response_cache_lock:
            _usage_history_response_cache["refreshing"] = False


def start_usage_history_response_refresh():
    with _usage_history_response_cache_lock:
        if _usage_history_response_cache["refreshing"]:
            return False
        _usage_history_response_cache["refreshing"] = True
    threading.Thread(target=refresh_usage_history_response_cache, daemon=True).start()
    return True


@app.route("/api/usage-history")
def get_usage_history():
    """Get historical usage data with aggregations."""
    now = time.time()
    with _usage_history_response_cache_lock:
        cached = _usage_history_response_cache["data"]
        last_update = _usage_history_response_cache["last_update"]
        ttl = _usage_history_response_cache["ttl"]

    if usage_history_cache_usable(cached) and (now - last_update) < ttl:
        return jsonify({**cached, "cache_age_seconds": round(now - last_update, 3), "cache_status": "memory"})
    if usage_history_cache_usable(cached):
        start_usage_history_response_refresh()
        return jsonify({**cached, "cache_age_seconds": round(now - last_update, 3), "refreshing": True, "cache_status": "stale_memory"})

    disk_cached, disk_mtime = load_usage_history_response_cache_from_disk()
    if disk_cached:
        with _usage_history_response_cache_lock:
            _usage_history_response_cache["data"] = disk_cached
            _usage_history_response_cache["last_update"] = disk_mtime
        start_usage_history_response_refresh()
        return jsonify({**disk_cached, "cache_age_seconds": round(now - disk_mtime, 3), "refreshing": True, "cache_status": "disk"})

    try:
        data = build_usage_history_response()
        save_usage_history_response_cache_to_disk(data)
        with _usage_history_response_cache_lock:
            _usage_history_response_cache["data"] = data
            _usage_history_response_cache["last_update"] = time.time()
        return jsonify({**data, "cache_age_seconds": 0, "cache_status": "live"})
    except Exception as e:
        print(f"[UsageHistory] Live refresh failed: {e}")
        start_usage_history_response_refresh()
        return jsonify({**usage_history_empty_response(refreshing=True), "error": str(e)})


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


def current_portal_session():
    return portal_state.get_session(request.cookies.get(SESSION_COOKIE_NAME, ""))


def _normalize_email(value):
    return portal_auth.normalize_email(value)


def portal_admin_emails():
    return portal_auth.admin_emails(config)


def is_admin_email(email):
    return portal_auth.is_admin_email(email, config)


def current_portal_user():
    return portal_auth.session_context(current_portal_session(), config)


def current_user_email():
    session_data = current_portal_user()
    return _normalize_email((session_data or {}).get("email"))


def current_user_name():
    session_data = current_portal_user() or {}
    user = session_data.get("user") or {}
    return str(user.get("name") or user.get("email") or session_data.get("email") or "").strip()


def is_current_admin():
    session_data = current_portal_user()
    return bool(session_data and session_data.get("is_admin"))


def user_can_access_email(email):
    return portal_auth.can_access_email(current_portal_user(), email)


def find_key_owner(api_key, user_data=None):
    if not api_key:
        return ""
    data = user_data or load_user_keys()
    key_info = data.get("keys", {}).get(api_key) or {}
    return _normalize_email(key_info.get("email"))


def user_can_access_key(api_key, user_data=None):
    owner = find_key_owner(api_key, user_data)
    current = current_user_email()
    return bool(current and owner and (is_current_admin() or owner == current))


PUBLIC_PATHS = {
    "/register",
    "/contribute",
    "/login",
    "/api/feishu/login-url",
    "/feishu/callback",
    "/api/session",
    "/api/logout",
    "/api/feishu/approval-callback",
    "/callback",
    "/favicon.ico",
}
PUBLIC_PREFIXES = ("/static/",)

ADMIN_PAGE_PREFIXES = ("/admin/",)
ADMIN_PAGE_PATHS = set()
ADMIN_API_PATHS = {
    "/api/accounts",
    "/api/all-users-stats",
    "/api/auth-stats",
    "/api/auth-stats/toggle-auth",
    "/api/check-expiry",
    "/api/keys",
    "/api/send-notification",
    "/api/sync-usage",
    "/api/update-key-email",
}


def is_public_request():
    if request.method == "OPTIONS":
        return True
    path = request.path or "/"
    if path in PUBLIC_PATHS:
        return True
    return any(path.startswith(prefix) for prefix in PUBLIC_PREFIXES)


def is_admin_request():
    path = request.path or "/"
    if path in ADMIN_PAGE_PATHS or any(path.startswith(prefix) for prefix in ADMIN_PAGE_PREFIXES):
        return True
    return path in ADMIN_API_PATHS


@app.before_request
def require_portal_login():
    if is_public_request():
        return None
    session_data = current_portal_user()
    if session_data:
        if is_admin_request() and not session_data.get("is_admin"):
            if (request.path or "").startswith("/api/"):
                return jsonify({"error": "需要管理员权限"}), 403
            return "需要管理员权限", 403
        return None
    if (request.path or "").startswith("/api/"):
        return jsonify({"error": "未登录", "login_url": "/login"}), 401
    next_url = request.full_path if request.query_string else request.path
    return redirect("/login?" + urlencode({"next": _safe_next_url(next_url, "/")}))


def upsert_feishu_user(user_info):
    email = str(user_info.get("email") or "").strip().lower()
    if not email:
        return None

    user_data = load_user_keys()
    users = user_data.setdefault("users", {})
    existing = users.get(email, {})
    now = datetime.utcnow().isoformat() + "Z"
    users[email] = {
        **existing,
        "email": email,
        "name": user_info.get("name") or user_info.get("en_name") or existing.get("name") or email,
        "api_keys": existing.get("api_keys", []),
        "created_at": existing.get("created_at") or now,
        "updated_at": now,
        "last_login_at": now,
        "feishu_open_id": user_info.get("open_id") or existing.get("feishu_open_id", ""),
        "feishu_user_id": user_info.get("user_id") or existing.get("feishu_user_id", ""),
        "feishu_union_id": user_info.get("union_id") or existing.get("feishu_union_id", ""),
        "avatar_url": user_info.get("avatar_url") or existing.get("avatar_url", ""),
    }
    if save_user_keys(user_data):
        return users[email]
    return None


def claim_api_keys_for_user(email, name, api_keys):
    email = str(email or "").strip().lower()
    if not email:
        return {"claimed": 0, "moved": 0, "created": 0, "keys": []}

    cleaned = []
    seen = set()
    for api_key in api_keys or []:
        text = str(api_key or "").strip()
        if not text or text in seen:
            continue
        seen.add(text)
        cleaned.append(text)
        if len(cleaned) >= 50:
            break

    if not cleaned:
        return {"claimed": 0, "moved": 0, "created": 0, "keys": []}

    user_data = load_user_keys()
    users = user_data.setdefault("users", {})
    keys_index = user_data.setdefault("keys", {})
    now = datetime.utcnow().isoformat() + "Z"
    user = users.setdefault(email, {
        "email": email,
        "name": name or email,
        "api_keys": [],
        "created_at": now,
    })
    user["name"] = user.get("name") or name or email
    user.setdefault("api_keys", [])

    moved = 0
    created = 0
    claimed = []
    for api_key in cleaned:
        key_info = keys_index.get(api_key)
        old_email = str((key_info or {}).get("email", "")).strip().lower()
        if key_info is None:
            key_info = {
                "email": email,
                "label": "浏览器导入",
                "model_group": "common",
                "source": "browser_claim",
                "created_at": now,
            }
            keys_index[api_key] = key_info
            created += 1
        else:
            if old_email and old_email != email:
                old_user = users.get(old_email)
                if old_user:
                    old_user["api_keys"] = [key for key in old_user.get("api_keys", []) if key != api_key]
                db.reassign_user_usage_key(api_key, email)
                moved += 1
            key_info["email"] = email
            key_info["updated_at"] = now
        if api_key not in user["api_keys"]:
            user["api_keys"].append(api_key)
        claimed.append(api_key)

    pool = load_key_pool()
    for api_key in claimed:
        if api_key in pool.get("assigned", {}):
            pool["assigned"][api_key] = email
    saved = save_user_keys(user_data)
    if saved:
        save_key_pool(pool)
    return {"claimed": len(claimed), "moved": moved, "created": created, "keys": claimed}


def feishu_oauth_redirect_uri():
    return f"{_public_base_url()}/feishu/callback"


def build_feishu_login_url(next_url="/"):
    state = portal_state.create_oauth_state({"next": _safe_next_url(next_url, "/")})
    query = urlencode({
        "client_id": config.FEISHU_APP_ID,
        "response_type": "code",
        "redirect_uri": feishu_oauth_redirect_uri(),
        "state": state,
    })
    return f"https://accounts.feishu.cn/open-apis/authen/v1/authorize?{query}"


def exchange_feishu_login_code(code):
    try:
        token_resp = requests.post(
            "https://open.feishu.cn/open-apis/authen/v2/oauth/token",
            headers={"Content-Type": "application/json; charset=utf-8"},
            json={
                "grant_type": "authorization_code",
                "client_id": config.FEISHU_APP_ID,
                "client_secret": config.FEISHU_APP_SECRET,
                "code": code,
                "redirect_uri": feishu_oauth_redirect_uri(),
            },
            timeout=10,
        )
        token_data = token_resp.json()
        if token_data.get("code") != 0:
            err = token_data.get("error_description") or token_data.get("msg") or token_data.get("error") or "飞书授权换 token 失败"
            print(f"[FeishuLogin] token exchange failed: code={token_data.get('code')} error={err}")
            return None, err
        access_token = token_data.get("access_token") or (token_data.get("data") or {}).get("access_token")
        if not access_token:
            print(f"[FeishuLogin] token exchange missing access_token: keys={list(token_data.keys())}")
            return None, "飞书授权响应缺少 access_token"

        info_resp = requests.get(
            "https://open.feishu.cn/open-apis/authen/v1/user_info",
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=10,
        )
        info_data = info_resp.json()
        if info_data.get("code") != 0:
            err = info_data.get("error_description") or info_data.get("msg") or info_data.get("error") or "获取飞书用户信息失败"
            print(f"[FeishuLogin] user info failed: code={info_data.get('code')} error={err}")
            return None, err
        user = info_data.get("data") or {}
        if not user.get("email") and user.get("enterprise_email"):
            user["email"] = user["enterprise_email"]
        if not user.get("email"):
            return None, "飞书用户信息没有返回邮箱，请检查应用权限"
        return user, None
    except Exception as exc:
        return None, f"飞书登录失败: {exc}"


@app.route("/api/feishu/login-url")
def feishu_login_url():
    next_url = _safe_next_url(request.args.get("next", "/"), "/")
    if not config.FEISHU_APP_ID or not config.FEISHU_APP_SECRET:
        return jsonify({"error": "Feishu app credentials are not configured"}), 500
    return jsonify({"url": build_feishu_login_url(next_url), "redirect_uri": feishu_oauth_redirect_uri()})


@app.route("/feishu/callback")
def feishu_login_callback():
    code = request.args.get("code", "").strip()
    state = request.args.get("state", "").strip()
    state_payload = portal_state.consume_oauth_state(state)
    if not code or state_payload is None:
        return redirect("/login?login_error=invalid_state")

    user_info, error = exchange_feishu_login_code(code)
    if error:
        return redirect(f"/login?login_error={quote(error)}")

    user = upsert_feishu_user(user_info)
    if not user:
        return redirect("/login?login_error=save_failed")

    ttl = max(1, int(getattr(config, "KEY_PORTAL_SESSION_DAYS", 30) or 30)) * 24 * 3600
    session_id = portal_state.create_session(
        user["email"],
        user,
        ttl_seconds=ttl,
        request_meta={"ip": request.remote_addr or "", "user_agent": request.headers.get("User-Agent", "")[:300]},
    )
    response = redirect(_safe_next_url(state_payload.get("next"), "/"))
    response.set_cookie(
        SESSION_COOKIE_NAME,
        session_id,
        max_age=ttl,
        httponly=True,
        secure=request.is_secure,
        samesite="Lax",
        path="/",
    )
    return response


@app.route("/api/session")
def api_session():
    session_data = current_portal_user()
    if not session_data:
        return jsonify({"authenticated": False})
    return jsonify({"authenticated": True, **session_data})


@app.route("/api/logout", methods=["POST"])
def api_logout():
    portal_state.destroy_session(request.cookies.get(SESSION_COOKIE_NAME, ""))
    response = jsonify({"success": True})
    response.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return response


@app.route("/api/session/claim-local-keys", methods=["POST"])
def api_claim_local_keys():
    session_data = current_portal_session()
    if not session_data:
        return jsonify({"error": "未登录"}), 401
    body = request.get_json(silent=True) or {}
    raw_keys = body.get("keys", [])
    if not isinstance(raw_keys, list):
        return jsonify({"error": "keys must be a list"}), 400
    user = session_data.get("user") or {}
    result = claim_api_keys_for_user(session_data["email"], user.get("name") or session_data["email"], raw_keys)
    return jsonify({"success": True, **result})


@app.route("/api/register-key", methods=["POST"])
def register_key():
    """Register a new user and assign an API key."""
    data = request.get_json() or {}
    session_data = current_portal_user()
    email = _normalize_email((session_data or {}).get("email"))
    name = current_user_name() or email
    label = data.get("label", "").strip()
    raw_model_group = str(data.get("model_group", "common") or "common").strip().lower()
    if raw_model_group == "gemini":
        return jsonify({"error": "Gemini Key 暂停申请"}), 400
    model_group = normalize_model_group(raw_model_group)
    reason = data.get("reason", "").strip()
    daily_budget = data.get("daily_budget", "").strip()

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

    if approval.requires_approval(model_group):
        if not reason:
            return jsonify({"error": "申请该模型组需要填写申请理由"}), 400
        if not daily_budget:
            return jsonify({"error": "请填写额度"}), 400
        instance_id, error = approval.create_approval_request(
            email, name, label, model_group, reason, daily_budget
        )
        if error:
            return jsonify({"error": error}), 500
        return jsonify({
            "success": True,
            "pending_approval": True,
            "instance_id": instance_id,
            "email": email,
            "model_group": model_group,
            "message": "已提交审批，审批通过后将自动生成 Key 并通过飞书通知你。"
        })

    # No approval needed — issue key directly
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


@app.route("/api/keys/topup", methods=["POST"])
def request_key_topup():
    data = request.get_json() or {}
    api_key = data.get("api_key", "").strip()
    reason = data.get("reason", "").strip()
    additional_budget = _parse_positive_budget(data.get("additional_budget"))

    if not api_key:
        return jsonify({"error": "请提供 API Key"}), 400
    if additional_budget is None:
        return jsonify({"error": "请填写有效的追加额度"}), 400
    if not reason:
        return jsonify({"error": "请填写追加额度原因"}), 400

    user_data = load_user_keys()
    key_info = user_data.get("keys", {}).get(api_key)
    if not key_info:
        return jsonify({"error": "Key 不存在"}), 404

    email = str(key_info.get("email", "")).strip().lower()
    if not user_can_access_email(email):
        return jsonify({"error": "不能操作其他用户的 Key"}), 403
    user = user_data.get("users", {}).get(email, {})
    if api_key not in user.get("api_keys", []):
        return jsonify({"error": "Key 归属校验失败"}), 403

    model_group = normalize_model_group(key_info.get("model_group", "common"))
    if not approval.requires_approval(model_group):
        return jsonify({"error": "该类型 Key 不需要申请追加额度"}), 400
    if key_info.get("source") != "litellm":
        return jsonify({"error": "该 Key 不支持自动追加额度"}), 400

    pending = approval.get_pending_approvals(email)
    for row in pending:
        if row.get("status") != "pending" or row.get("request_type") != approval.REQUEST_TYPE_QUOTA_TOPUP:
            continue
        try:
            payload = json.loads(row.get("request_payload") or "{}")
        except Exception:
            payload = {}
        if payload.get("api_key") == api_key:
            return jsonify({"error": "该 Key 已有待审批的追加额度申请"}), 400

    current_budget = _key_budget(key_info, api_key)
    if current_budget is None:
        return jsonify({"error": "无法识别该 Key 当前预算"}), 400

    label = key_info.get("label", "")
    name = user.get("name") or email
    instance_id, error = approval.create_quota_topup_request(
        email, name, label, model_group, api_key, current_budget, additional_budget, reason
    )
    if error:
        return jsonify({"error": error}), 500

    return jsonify({
        "success": True,
        "pending_approval": True,
        "instance_id": instance_id,
        "email": email,
        "model_group": model_group,
        "current_budget": current_budget,
        "additional_budget": additional_budget,
        "new_budget": round(current_budget + additional_budget, 6),
        "message": "已提交追加额度审批，审批通过后会自动更新当前 Key。"
    })


@app.route("/api/feishu/approval-callback", methods=["POST"])
def feishu_approval_callback():
    """Receive Feishu approval status change webhook."""
    body_bytes = request.get_data()
    body_str = body_bytes.decode("utf-8")

    try:
        payload = json.loads(body_str)
    except Exception:
        return jsonify({"code": 1, "msg": "invalid json"}), 400

    # Handle Feishu URL verification challenge
    if payload.get("type") == "url_verification":
        return jsonify({"challenge": payload.get("challenge", "")})

    # Verify token if configured
    header = payload.get("header", {})
    token = header.get("token", "")
    if not approval.verify_callback_token(token):
        return jsonify({"code": 2, "msg": "token mismatch"}), 403

    event_type = header.get("event_type", "") or payload.get("event", {}).get("type", "")
    if "approval_instance" in event_type or "approval" in event_type:
        success, msg = approval.handle_approval_callback(payload)
        return jsonify({"code": 0, "msg": msg})

    return jsonify({"code": 0, "msg": "ignored"})


@app.route("/api/my-approvals", methods=["POST"])
def get_my_approvals():
    """Get approval history for a user."""
    data = request.get_json() or {}
    email = _normalize_email(data.get("email"))
    if not is_current_admin():
        email = current_user_email()
    if not email:
        return jsonify({"error": "请输入邮箱"}), 400
    if not user_can_access_email(email):
        return jsonify({"error": "不能查看其他用户审批记录"}), 403
    rows = approval.get_pending_approvals(email)
    return jsonify({"approvals": rows})


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
    data = request.get_json(silent=True) or {}
    email = _normalize_email(data.get("email"))
    if not is_current_admin():
        email = current_user_email()

    if not email:
        return jsonify({"error": "请输入邮箱"}), 400
    if not user_can_access_email(email):
        return jsonify({"error": "不能查看其他用户的 Key"}), 403

    user_data = load_user_keys()
    user = user_data["users"].get(email)

    if not user:
        return jsonify({"email": email, "name": email, "keys": []})

    key_totals = {row.get("key_id"): row for row in litellm_user_key_totals(email, user.get("api_keys", []))}

    keys_info = []
    for api_key in user.get("api_keys", []):
        key_meta = user_data["keys"].get(api_key, {})
        key_stats = key_totals.get(api_key) or key_totals.get(key_meta.get("label", "")) or {}

        max_budget = _key_budget(key_meta, api_key)
        keys_info.append({
            "key": api_key,
            "label": key_meta.get("label", ""),
            "model_group": key_meta.get("model_group", "common"),
            "source": key_meta.get("source", "cliproxy"),
            "max_budget": max_budget,
            "can_topup": key_meta.get("source") == "litellm" and approval.requires_approval(normalize_model_group(key_meta.get("model_group", "common"))) and max_budget is not None,
            "created_at": key_meta.get("created_at", ""),
            "total_requests": key_stats.get("total_requests", 0),
            "total_tokens": key_stats.get("total_tokens", 0),
            "spend_usd": round(_float_usage_value(key_stats.get("spend_usd", 0)), 6),
        })

    return jsonify({
        "email": email,
        "name": user.get("name", email),
        "keys": keys_info
    })


@app.route("/api/revoke-key", methods=["POST"])
def revoke_key_api():
    """Revoke a user's API key."""
    data = request.get_json(silent=True) or {}
    api_key = data.get("key", "").strip()

    if not api_key:
        return jsonify({"error": "请提供 API Key"}), 400
    if not user_can_access_key(api_key):
        return jsonify({"error": "不能撤销其他用户的 Key"}), 403

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
    if not user_can_access_email(email):
        return jsonify({"error": "不能查看其他用户的统计"}), 403

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
    spend_usd = round(sum(_float_usage_value(s.get("spend_usd", 0)) for s in stats), 6)
    token_breakdown = build_litellm_token_breakdown(total_tokens, input_tokens, output_tokens, cached_tokens, reasoning_tokens, spend_usd)
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
            "spend_usd": spend_usd,
            "total_keys": total_keys,
        },
        "aggregation": aggregation,
        "token_pricing": litellm_spend_pricing_metadata(),
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
    data = request.get_json(silent=True) or {}
    api_key = data.get("api_key", "").strip()

    if not api_key:
        return jsonify({"error": "请提供 API Key"}), 400

    user_data = load_user_keys()

    # Find which user owns this key
    key_info = user_data["keys"].get(api_key)
    if not key_info:
        return jsonify({"error": "Key 不存在"}), 404
    if not user_can_access_key(api_key, user_data):
        return jsonify({"error": "不能查看其他用户的 Key"}), 403

    identifier = key_info["email"]
    user = user_data["users"].get(identifier)

    if not user:
        return jsonify({"error": "用户不存在"}), 404

    today = beijing_today()
    key_rows = litellm_user_key_totals(identifier, user.get("api_keys", []))
    key_totals = {row.get("key_id"): row for row in key_rows}

    user_total_requests = 0
    user_total_tokens = 0
    user_total_input_tokens = 0
    user_total_output_tokens = 0
    user_total_cached_tokens = 0
    user_total_reasoning_tokens = 0
    user_spend_usd = 0.0
    user_today_requests = 0
    user_today_tokens = 0
    user_today_input_tokens = 0
    user_today_output_tokens = 0
    user_today_cached_tokens = 0
    user_today_reasoning_tokens = 0
    user_today_spend_usd = 0.0
    queried_key = {}
    all_keys = []

    for key in user.get("api_keys", []):
        key_meta = user_data["keys"].get(key, {})
        key_stats = key_totals.get(key) or key_totals.get(key_meta.get("label", "")) or {}
        max_budget = _key_budget(key_meta, key)
        requests = _int_usage_value(key_stats.get("total_requests"))
        tokens = _int_usage_value(key_stats.get("total_tokens"))
        input_tokens = _int_usage_value(key_stats.get("input_tokens"))
        output_tokens = _int_usage_value(key_stats.get("output_tokens"))
        cached_tokens = _int_usage_value(key_stats.get("cached_tokens"))
        reasoning_tokens = _int_usage_value(key_stats.get("reasoning_tokens"))
        spend_usd = round(_float_usage_value(key_stats.get("spend_usd")), 6)
        today_requests = _int_usage_value(key_stats.get("today_requests"))
        today_tokens = _int_usage_value(key_stats.get("today_tokens"))
        today_input_tokens = _int_usage_value(key_stats.get("today_input_tokens"))
        today_output_tokens = _int_usage_value(key_stats.get("today_output_tokens"))
        today_cached_tokens = _int_usage_value(key_stats.get("today_cached_tokens"))
        today_reasoning_tokens = _int_usage_value(key_stats.get("today_reasoning_tokens"))
        today_spend_usd = round(_float_usage_value(key_stats.get("today_spend_usd")), 6)
        breakdown = build_litellm_token_breakdown(tokens, input_tokens, output_tokens, cached_tokens, reasoning_tokens, spend_usd)
        today_breakdown = build_litellm_token_breakdown(today_tokens, today_input_tokens, today_output_tokens, today_cached_tokens, today_reasoning_tokens, today_spend_usd)

        user_total_requests += requests
        user_total_tokens += tokens
        user_total_input_tokens += input_tokens
        user_total_output_tokens += output_tokens
        user_total_cached_tokens += cached_tokens
        user_total_reasoning_tokens += reasoning_tokens
        user_spend_usd += spend_usd
        user_today_requests += today_requests
        user_today_tokens += today_tokens
        user_today_input_tokens += today_input_tokens
        user_today_output_tokens += today_output_tokens
        user_today_cached_tokens += today_cached_tokens
        user_today_reasoning_tokens += today_reasoning_tokens
        user_today_spend_usd += today_spend_usd

        item = {
            "key": key,
            "label": key_meta.get("label", ""),
            "model_group": key_meta.get("model_group", "common"),
            "source": key_meta.get("source", "cliproxy"),
            "max_budget": max_budget,
            "can_topup": key_meta.get("source") == "litellm" and approval.requires_approval(normalize_model_group(key_meta.get("model_group", "common"))) and max_budget is not None,
            "created_at": key_meta.get("created_at", ""),
            "total_requests": requests,
            "total_tokens": tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "spend_usd": spend_usd,
            "token_breakdown": breakdown,
            "estimated_cost_usd": breakdown["cost_usd"],
            "today_requests": today_requests,
            "today_tokens": today_tokens,
            "today_input_tokens": today_input_tokens,
            "today_output_tokens": today_output_tokens,
            "today_cached_tokens": today_cached_tokens,
            "today_reasoning_tokens": today_reasoning_tokens,
            "today_spend_usd": today_spend_usd,
            "today_token_breakdown": today_breakdown,
        }
        if key == api_key:
            queried_key = item
        all_keys.append(item)

    queried_breakdown = queried_key.get("token_breakdown") or build_litellm_token_breakdown(0, 0, 0, 0, 0, 0)
    user_breakdown = build_litellm_token_breakdown(user_total_tokens, user_total_input_tokens, user_total_output_tokens, user_total_cached_tokens, user_total_reasoning_tokens, user_spend_usd)
    user_today_breakdown = build_litellm_token_breakdown(user_today_tokens, user_today_input_tokens, user_today_output_tokens, user_today_cached_tokens, user_today_reasoning_tokens, user_today_spend_usd)
    return jsonify({
        "identifier": identifier,
        "date": today,
        "total_requests": queried_key.get("total_requests", 0),
        "total_tokens": queried_key.get("total_tokens", 0),
        "input_tokens": queried_key.get("input_tokens", 0),
        "output_tokens": queried_key.get("output_tokens", 0),
        "cached_tokens": queried_key.get("cached_tokens", 0),
        "reasoning_tokens": queried_key.get("reasoning_tokens", 0),
        "spend_usd": queried_key.get("spend_usd", 0),
        "token_breakdown": queried_breakdown,
        "estimated_cost_usd": queried_breakdown["cost_usd"],
        "today_requests": queried_key.get("today_requests", 0),
        "today_tokens": queried_key.get("today_tokens", 0),
        "today_input_tokens": queried_key.get("today_input_tokens", 0),
        "today_output_tokens": queried_key.get("today_output_tokens", 0),
        "today_cached_tokens": queried_key.get("today_cached_tokens", 0),
        "today_reasoning_tokens": queried_key.get("today_reasoning_tokens", 0),
        "today_spend_usd": queried_key.get("today_spend_usd", 0),
        "today_token_breakdown": queried_key.get("today_token_breakdown") or build_litellm_token_breakdown(0, 0, 0, 0, 0, 0),
        "user_total_requests": user_total_requests,
        "user_total_tokens": user_total_tokens,
        "user_input_tokens": user_total_input_tokens,
        "user_output_tokens": user_total_output_tokens,
        "user_cached_tokens": user_total_cached_tokens,
        "user_reasoning_tokens": user_total_reasoning_tokens,
        "user_spend_usd": round(user_spend_usd, 6),
        "user_token_breakdown": user_breakdown,
        "user_estimated_cost_usd": user_breakdown["cost_usd"],
        "user_today_requests": user_today_requests,
        "user_today_tokens": user_today_tokens,
        "user_today_input_tokens": user_today_input_tokens,
        "user_today_output_tokens": user_today_output_tokens,
        "user_today_cached_tokens": user_today_cached_tokens,
        "user_today_reasoning_tokens": user_today_reasoning_tokens,
        "user_today_spend_usd": round(user_today_spend_usd, 6),
        "user_today_token_breakdown": user_today_breakdown,
        "all_keys": all_keys,
        "token_pricing": litellm_spend_pricing_metadata(),
    })


@app.route("/api/user-keys")
def api_get_user_keys():
    email = _normalize_email(request.args.get("email", ""))
    date = request.args.get("date", "").strip()

    if not is_current_admin():
        if email and email != current_user_email():
            return jsonify({"error": "不能查看其他用户的 Key"}), 403
        email = current_user_email()

    if not email:
        return jsonify({"error": "请提供用户标识"}), 400
    if date and not re.match(r"^\d{4}-\d{2}-\d{2}$", date):
        return jsonify({"error": "日期格式应为 YYYY-MM-DD"}), 400

    user_data = load_user_keys()
    user = user_data.get("users", {}).get(email)
    key_entries = find_user_key_entries(user_data, email)

    if not user and not key_entries:
        return jsonify({"error": "用户不存在"}), 404

    api_keys = [entry.get("key", "") for entry in key_entries if entry.get("key", "")]
    stats_rows = litellm_user_key_totals(email, api_keys)
    stats_by_key = {row.get("key_id"): row for row in stats_rows}

    keys_info = []
    for api_key in api_keys:
        key_meta = user_data.get("keys", {}).get(api_key, {})
        key_stats = stats_by_key.get(api_key) or stats_by_key.get(key_meta.get("label", "")) or {}
        if date:
            series = {row.get("date"): row for row in litellm_key_timeseries(api_key, date, date, email=email)}
            day_stats = series.get(date, {})
            key_stats = {
                **key_stats,
                "total_requests": day_stats.get("requests", 0),
                "success_count": day_stats.get("success_count", 0),
                "failure_count": day_stats.get("failure_count", 0),
                "total_tokens": day_stats.get("total_tokens", 0),
                "input_tokens": day_stats.get("input_tokens", 0),
                "output_tokens": day_stats.get("output_tokens", 0),
                "cached_tokens": day_stats.get("cached_tokens", 0),
                "reasoning_tokens": day_stats.get("reasoning_tokens", 0),
                "spend_usd": day_stats.get("spend_usd", 0),
            }
        spend_usd = round(_float_usage_value(key_stats.get("spend_usd", 0)), 6)
        breakdown = build_litellm_token_breakdown(key_stats.get("total_tokens", 0), key_stats.get("input_tokens", 0), key_stats.get("output_tokens", 0), key_stats.get("cached_tokens", 0), key_stats.get("reasoning_tokens", 0), spend_usd)
        max_budget = _key_budget(key_meta, api_key)
        keys_info.append({
            "key": api_key,
            "label": key_meta.get("label", ""),
            "model_group": key_meta.get("model_group", "common"),
            "source": key_meta.get("source", "cliproxy"),
            "max_budget": max_budget,
            "can_topup": key_meta.get("source") == "litellm" and approval.requires_approval(normalize_model_group(key_meta.get("model_group", "common"))) and max_budget is not None,
            "created_at": key_meta.get("created_at", ""),
            "total_requests": key_stats.get("total_requests", 0),
            "success_count": key_stats.get("success_count", 0),
            "failure_count": key_stats.get("failure_count", 0),
            "total_tokens": key_stats.get("total_tokens", 0),
            "input_tokens": key_stats.get("input_tokens", 0),
            "output_tokens": key_stats.get("output_tokens", 0),
            "cached_tokens": key_stats.get("cached_tokens", 0),
            "reasoning_tokens": key_stats.get("reasoning_tokens", 0),
            "spend_usd": spend_usd,
            "token_breakdown": breakdown,
            "estimated_cost_usd": breakdown["cost_usd"],
        })

    keys_info.sort(key=lambda item: item.get("total_tokens", 0), reverse=True)
    return jsonify({
        "email": email,
        "name": (user or {}).get("name", email),
        "date": date,
        "keys": keys_info,
        "token_pricing": litellm_spend_pricing_metadata(),
    })


@app.route("/api/user-monitor/recent-requests")
def api_user_monitor_recent_requests():
    email = _normalize_email(request.args.get("email", ""))
    api_key = request.args.get("api_key", "").strip()
    try:
        hours = int(request.args.get("hours", "24") or "24")
    except ValueError:
        hours = 24
    try:
        limit = int(request.args.get("limit", "100") or "100")
    except ValueError:
        limit = 100

    if not email and not api_key:
        return jsonify({"error": "请提供 email 或 API Key"}), 400

    user_data = load_user_keys()
    owner = ""
    if api_key:
        key_info = user_data.get("keys", {}).get(api_key)
        if not key_info:
            return jsonify({"error": "Key 不存在"}), 404
        owner = _normalize_email(key_info.get("email"))
        if not user_can_access_key(api_key, user_data):
            return jsonify({"error": "不能查看其他用户的 Key"}), 403
        if email and owner and owner != email:
            return jsonify({"error": "该 Key 不属于该用户"}), 403
    else:
        if not user_can_access_email(email):
            return jsonify({"error": "不能查看其他用户的监控"}), 403
        owner = email

    rows = []
    for row in litellm_recent_requests(api_key=api_key, email=owner if not api_key else "", hours=hours, limit=limit):
        spend_usd = round(_float_usage_value(row.get("spend_usd", 0)), 6)
        total_tokens = int(row.get("total_tokens", 0) or 0)
        input_tokens = int(row.get("input_tokens", 0) or 0)
        output_tokens = int(row.get("output_tokens", 0) or 0)
        cached_tokens = int(row.get("cached_tokens", 0) or 0)
        reasoning_tokens = int(row.get("reasoning_tokens", 0) or 0)
        breakdown = build_litellm_token_breakdown(total_tokens, input_tokens, output_tokens, cached_tokens, reasoning_tokens, spend_usd)
        rows.append({
            "start_time": row.get("start_time"),
            "end_time": row.get("end_time"),
            "key_label": row.get("key_label"),
            "user_email": row.get("user_email"),
            "model": row.get("model"),
            "status": row.get("status"),
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "spend_usd": spend_usd,
            "token_breakdown": breakdown,
            "estimated_cost_usd": breakdown["cost_usd"],
            "latency_ms": round(_float_usage_value(row.get("latency_ms", 0)), 2),
            "call_type": row.get("call_type") or "",
            "request_id": row.get("request_id") or "",
            "user_api_base": row.get("user_api_base") or "",
        })

    return jsonify({
        "email": owner,
        "api_key": mask_api_key(api_key) if api_key else "",
        "hours": max(1, min(hours, 168)),
        "limit": max(1, min(limit, 500)),
        "requests": rows,
        "source": "litellm_spendlogs",
        "note": "仅返回 LiteLLM 元数据，不包含请求体或响应体。",
    })


@app.route("/api/user-monitor/reconcile")
def api_user_monitor_reconcile():
    api_key = request.args.get("api_key", "").strip()
    try:
        hours = int(request.args.get("hours", "24") or "24")
    except ValueError:
        hours = 24
    try:
        limit = int(request.args.get("limit", "500") or "500")
    except ValueError:
        limit = 500

    if not api_key:
        return jsonify({"error": "请提供 API Key"}), 400

    user_data = load_user_keys()
    key_info = user_data.get("keys", {}).get(api_key)
    if not key_info:
        return jsonify({"error": "Key 不存在"}), 404
    if not user_can_access_key(api_key, user_data):
        return jsonify({"error": "不能查看其他用户的 Key"}), 403

    litellm_rows = litellm_recent_requests(api_key=api_key, hours=hours, limit=limit)
    monitor_rows = monitor_log_recent_entries(api_key, hours=hours, limit=limit)
    litellm_failures = sum(1 for row in litellm_rows if str(row.get("status") or "success").lower() == "failure")
    monitor_failures = sum(1 for row in monitor_rows if str(row.get("status") or "").startswith(("4", "5")))
    litellm_models = sorted({str(row.get("model") or "") for row in litellm_rows if row.get("model")})
    monitor_paths = sorted({str(row.get("url") or "").split("?", 1)[0] for row in monitor_rows if row.get("url")})
    litellm_count = len(litellm_rows)
    monitor_count = len(monitor_rows)
    difference = litellm_count - monitor_count
    denominator = max(litellm_count, monitor_count, 1)

    return jsonify({
        "api_key": mask_api_key(api_key),
        "email": _normalize_email(key_info.get("email")),
        "hours": max(1, min(hours, 168)),
        "limit": max(1, min(limit, 5000)),
        "litellm": {
            "source": "litellm_spendlogs",
            "requests": litellm_count,
            "failures": litellm_failures,
            "models": litellm_models,
        },
        "monitor_logs": {
            "source": "core_monitor_logs_metadata",
            "requests": monitor_count,
            "failures": monitor_failures,
            "paths": monitor_paths,
            "log_dir_exists": os.path.isdir(os.path.abspath(MONITOR_LOG_DIR)),
        },
        "difference": difference,
        "difference_ratio": round(abs(difference) / denominator, 6),
        "within_one_percent": abs(difference) / denominator < 0.01,
        "note": "monitor_logs 只解析旧日志元数据，不返回请求体或响应体。",
    })


@app.route("/api/user-key-timeseries")
def api_get_user_key_timeseries():
    email = _normalize_email(request.args.get("email", ""))
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
    owner = _normalize_email(key_info.get("email", email))
    if not is_current_admin() and owner != current_user_email():
        return jsonify({"error": "不能查看其他用户的 Key"}), 403
    if email and owner and owner != email and owner.lower() != email.lower():
        return jsonify({"error": "该 Key 不属于该用户"}), 403

    hourly = num_days == 1
    cache_key = f"{owner or email}|{api_key}|{date_from}|{date_to}|{'hourly' if hourly else 'daily'}"
    now = time.time()
    with _user_key_timeseries_cache_lock:
        cached = _user_key_timeseries_cache["data"].get(cache_key)
        if cached and (now - cached["last_update"]) < _user_key_timeseries_cache["ttl"]:
            return jsonify(cached["data"])

    if not key_info:
        return jsonify({"error": "Key 不存在"}), 404

    buckets = []
    for row in litellm_key_timeseries(api_key, date_from, date_to, email=owner or email, hourly=hourly):
        total_tokens = int(row.get("total_tokens", 0) or 0)
        input_tokens = int(row.get("input_tokens", 0) or 0)
        output_tokens = int(row.get("output_tokens", 0) or 0)
        cached_tokens = int(row.get("cached_tokens", 0) or 0)
        reasoning_tokens = int(row.get("reasoning_tokens", 0) or 0)
        spend_usd = round(_float_usage_value(row.get("spend_usd", 0)), 6)
        breakdown = build_litellm_token_breakdown(total_tokens, input_tokens, output_tokens, cached_tokens, reasoning_tokens, spend_usd)
        buckets.append({
            "date": row.get("date"),
            "hour": row.get("hour", ""),
            "requests": int(row.get("requests", 0) or 0),
            "success_count": int(row.get("success_count", 0) or 0),
            "failure_count": int(row.get("failure_count", 0) or 0),
            "total_tokens": total_tokens,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "cached_tokens": cached_tokens,
            "reasoning_tokens": reasoning_tokens,
            "spend_usd": spend_usd,
            "token_breakdown": breakdown,
            "estimated_cost_usd": breakdown["cost_usd"],
        })
    mode = "hourly" if hourly else "daily"

    totals = {
        "requests": sum(b["requests"] for b in buckets),
        "success_count": sum(b["success_count"] for b in buckets),
        "failure_count": sum(b["failure_count"] for b in buckets),
        "total_tokens": sum(b["total_tokens"] for b in buckets),
        "input_tokens": sum(b["input_tokens"] for b in buckets),
        "output_tokens": sum(b["output_tokens"] for b in buckets),
        "cached_tokens": sum(b["cached_tokens"] for b in buckets),
        "reasoning_tokens": sum(b["reasoning_tokens"] for b in buckets),
        "spend_usd": round(sum(_float_usage_value(b.get("spend_usd", 0)) for b in buckets), 6),
    }
    totals_breakdown = build_litellm_token_breakdown(totals["total_tokens"], totals["input_tokens"], totals["output_tokens"], totals["cached_tokens"], totals["reasoning_tokens"], totals["spend_usd"])
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
        "token_pricing": litellm_spend_pricing_metadata(),
    }
    with _user_key_timeseries_cache_lock:
        _user_key_timeseries_cache["data"][cache_key] = {
            "data": result,
            "last_update": time.time(),
        }
    return jsonify(result)


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


def broadcast_usage_update(force=False):
    usage_broadcaster.broadcast(force=force)


# WebSocket event handlers
@socketio.on("connect")
def handle_connect():
    """Handle client connection."""
    usage_broadcaster.handle_connect()


@socketio.on("disconnect")
def handle_disconnect():
    """Handle client disconnection."""
    usage_broadcaster.handle_disconnect()


def scheduled_nlb_health_monitor():
    """Check NLB node readiness and notify on state changes."""
    with app.app_context():
        try:
            events = nlb_monitor.monitor_once(CLIPROXY_NODES)
            for event in events:
                status_service.record_nlb_monitor_event(event)
                print(f"[NLBMonitor] {event.get('node')} -> {event.get('status')}: {event.get('reason')}")
        except Exception as e:
            print(f"[NLBMonitor] Error: {e}")


def scheduled_usage_record_monitor():
    """Publish an announcement when today's usage crosses the historical daily record."""
    with app.app_context():
        try:
            saved = status_service.check_daily_usage_record()
            if saved and saved.get("created"):
                print(f"[Status] Usage record announced: {(saved.get('event') or {}).get('summary')}")
        except Exception as e:
            print(f"[Status] Usage record monitor error: {e}")


def scheduled_model_group_spend_monitor():
    """Publish an alert when an expensive model group crosses the daily spend threshold."""
    with app.app_context():
        try:
            results = status_service.check_model_group_spend_alerts()
            created = [item for item in (results or []) if item and item.get("created")]
            for item in created:
                print(f"[Status] Model group spend alert: {(item.get('event') or {}).get('summary')}")
        except Exception as e:
            print(f"[Status] Model group spend monitor error: {e}")


if __name__ == "__main__":
    # Load data on startup
    print("[Startup] Initializing Key Portal state backend...")
    portal_state.ensure_schema()

    print("[Startup] Initializing database...")
    db.init_database()

    print("[Startup] Initializing approval table...")
    approval.init_approval_table()

    print("[Startup] Loading user keys database...")
    load_user_keys()

    print("[Startup] Restoring CLIProxyAPI usage snapshots...")
    # import_cliproxy_snapshot()  # disabled: NLB architecture, nodes are independent

    portal_scheduler.configure_scheduler(scheduler, config, {
        "expiry_check": scheduled_expiry_check,
        "snapshot_export": scheduled_snapshot_export,
        "usage_broadcast": broadcast_usage_update,
        "approval_poll": lambda: approval.poll_pending_approvals(),
        "nlb_health_monitor": scheduled_nlb_health_monitor,
        "usage_record_monitor": scheduled_usage_record_monitor,
        "model_group_spend_monitor": scheduled_model_group_spend_monitor,
    })
    scheduler.start()
    portal_scheduler.print_schedule(config)

    # Initial usage sync is manual-only because the full usage payload is large.

    # Run Flask app with SocketIO
    print(f"Starting Key Portal on {config.HOST}:{config.PORT} (WebSocket enabled)")
    socketio.run(app, host=config.HOST, port=config.PORT, debug=False, allow_unsafe_werkzeug=True)
