"""
Feishu notification module.
Handles Feishu access token and message sending.
"""

import json
import requests
import config


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
                                    "url": "http://172.16.70.100:8080/login"
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
