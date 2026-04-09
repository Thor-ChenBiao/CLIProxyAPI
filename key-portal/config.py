# CLIProxyAPI Key Portal Configuration

import os

# CLIProxyAPI Management API
CLIPROXY_API_URL = "http://localhost:8317"
CLIPROXY_MANAGEMENT_KEY = "admin123"

# Feishu App credentials for sending notifications
FEISHU_APP_ID = "cli_a23fe3b0b6fa900b"
FEISHU_APP_SECRET = "3I6GxOUWak70VjVnYF39nnX57N7kNnuS"

# Key expiry warning threshold (hours before expiry to send notification)
KEY_EXPIRE_WARNING_HOURS = 2

# Check interval for key expiry (minutes)
KEY_CHECK_INTERVAL_MINUTES = 30

# Server settings
HOST = "0.0.0.0"
PORT = 8080

# Service info for tutorial page
_public_base = os.environ.get("PUBLIC_BASE_URL", "").strip()
if _public_base:
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
