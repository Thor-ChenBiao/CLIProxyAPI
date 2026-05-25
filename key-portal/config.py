# CLIProxyAPI Key Portal Configuration

import os

# CLIProxyAPI Management API
CLIPROXY_API_URL = "http://localhost:8317"
CLIPROXY_MANAGEMENT_KEY = "admin123"

# Feishu App credentials for login and notifications. Production reuses the
# approval/message app from systemd unless FEISHU_APP_ID is explicitly set.
FEISHU_APP_ID = os.environ.get("FEISHU_APP_ID", os.environ.get("FEISHU_APPROVAL_APP_ID", "cli_a23fe3b0b6fa900b"))
FEISHU_APP_SECRET = os.environ.get("FEISHU_APP_SECRET", os.environ.get("FEISHU_APPROVAL_APP_SECRET", "3I6GxOUWak70VjVnYF39nnX57N7kNnuS"))

# LiteLLM virtual key issuance and persisted usage aggregates
LITELLM_API_URL = os.environ.get("LITELLM_API_URL", "http://127.0.0.1:4000").strip().rstrip("/")
LITELLM_MASTER_KEY = os.environ.get("LITELLM_MASTER_KEY", "").strip()
LITELLM_ISSUE_KEYS = os.environ.get("LITELLM_ISSUE_KEYS", "").strip().lower() in {"1", "true", "yes", "on"}
LITELLM_DATABASE_URL = os.environ.get("LITELLM_DATABASE_URL", os.environ.get("DATABASE_URL", "")).strip()

# Key Portal state backends reuse the existing service database/cache by
# default. KEY_PORTAL_* can still override them if we split storage later.
KEY_PORTAL_DATABASE_URL = os.environ.get(
    "KEY_PORTAL_DATABASE_URL", os.environ.get("DATABASE_URL", "")
).strip()
KEY_PORTAL_REDIS_URL = os.environ.get(
    "KEY_PORTAL_REDIS_URL", os.environ.get("REDIS_URL", "")
).strip()
try:
    KEY_PORTAL_SESSION_DAYS = int(os.environ.get("KEY_PORTAL_SESSION_DAYS", "30") or "30")
except ValueError:
    KEY_PORTAL_SESSION_DAYS = 30
KEY_PORTAL_SNAPSHOT_EXPORT_ENABLED = os.environ.get(
    "KEY_PORTAL_SNAPSHOT_EXPORT_ENABLED", "false"
).strip().lower() in {"1", "true", "yes", "on"}
KEY_PORTAL_ADMIN_EMAILS = [
    email.strip().lower()
    for email in os.environ.get(
        "KEY_PORTAL_ADMIN_EMAILS",
        "biao.chen@zilliz.com,xiaofan.luan@zilliz.com",
    ).split(",")
    if email.strip()
]

# Feishu Approval — separate app from the notification app above
FEISHU_APPROVAL_APP_ID = os.environ.get("FEISHU_APPROVAL_APP_ID", "")
FEISHU_APPROVAL_APP_SECRET = os.environ.get("FEISHU_APPROVAL_APP_SECRET", "")
FEISHU_APPROVAL_CODE = os.environ.get("FEISHU_APPROVAL_CODE", "FB411A4C-6C30-426B-AF4F-4D8892996EC9")
FEISHU_APPROVAL_ENCRYPT_KEY = os.environ.get("FEISHU_APPROVAL_ENCRYPT_KEY", "")
FEISHU_APPROVAL_VERIFICATION_TOKEN = os.environ.get("FEISHU_APPROVAL_VERIFICATION_TOKEN", "")
FEISHU_APPROVAL_CHAT_ID = os.environ.get("FEISHU_APPROVAL_CHAT_ID", "oc_32f686af50e056584a08eee787bc2dfe").strip()

# Key expiry warning threshold (hours before expiry to send notification)
KEY_EXPIRE_WARNING_HOURS = 2

# Check interval for key expiry (minutes)
KEY_CHECK_INTERVAL_MINUTES = 30

def _env_int(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _env_float(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _env_csv(name, default):
    value = os.environ.get(name, "").strip()
    if not value:
        value = default
    return [item.strip().lower() for item in value.split(",") if item.strip()]


# Server settings
HOST = os.environ.get("KEY_PORTAL_HOST", "0.0.0.0").strip() or "0.0.0.0"
PORT = _env_int("KEY_PORTAL_PORT", 8080)

# NLB health monitor settings. Only the full key-portal process uses these.
NLB_MONITOR_ENABLED = os.environ.get("NLB_MONITOR_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
NLB_MONITOR_INTERVAL_SECONDS = _env_int("NLB_MONITOR_INTERVAL_SECONDS", 60)

# Status announcements and incident notifications.
STATUS_FEISHU_WEBHOOK_URL = os.environ.get("STATUS_FEISHU_WEBHOOK_URL", "").strip()
STATUS_PUBLIC_URL = os.environ.get("STATUS_PUBLIC_URL", "").strip()
STATUS_NODE_HEALTH_ALERT_DELAY_SECONDS = _env_int("STATUS_NODE_HEALTH_ALERT_DELAY_SECONDS", 300)
STATUS_USAGE_RECORD_ENABLED = os.environ.get("STATUS_USAGE_RECORD_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
STATUS_USAGE_RECORD_LOOKBACK_DAYS = _env_int("STATUS_USAGE_RECORD_LOOKBACK_DAYS", 365)
MODEL_GROUP_SPEND_ALERT_ENABLED = os.environ.get("MODEL_GROUP_SPEND_ALERT_ENABLED", "true").strip().lower() in {"1", "true", "yes", "on"}
MODEL_GROUP_SPEND_ALERT_INTERVAL_MINUTES = _env_int("MODEL_GROUP_SPEND_ALERT_INTERVAL_MINUTES", 30)
MODEL_GROUP_SPEND_ALERT_THRESHOLD_USD = _env_float("MODEL_GROUP_SPEND_ALERT_THRESHOLD_USD", 100.0)
MODEL_GROUP_SPEND_ALERT_GROUPS = _env_csv("MODEL_GROUP_SPEND_ALERT_GROUPS", "claude,deepseek")

# Service info for tutorial page
_public_base = os.environ.get("PUBLIC_BASE_URL", "").strip()
_public_api_base = os.environ.get("PUBLIC_API_BASE_URL", "").strip()
if _public_api_base:
    _service_base_url = _public_api_base.rstrip("/")
elif _public_base:
    _service_base_url = _public_base.rstrip("/").replace(":8080", ":8317")
else:
    _public_host = os.environ.get("PUBLIC_HOST", "").strip()
    _service_base_url = f"http://{_public_host}:8317" if _public_host else "http://localhost:8317"

SERVICE_INFO = {
    "base_url": _service_base_url,
    "available_models": [
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-5-20251101",
        "claude-3-5-sonnet-20241022",
        "claude-3-5-haiku-20241022",
    ],
    "api_endpoints": {
        "chat": "/v1/chat/completions",
        "messages": "/v1/messages",
        "models": "/v1/models",
    }
}
