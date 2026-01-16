# CLIProxyAPI Key Portal Configuration

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
SERVICE_INFO = {
    "base_url": "http://172.16.70.100:8317",
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
