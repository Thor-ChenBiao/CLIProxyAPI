"""Usage timestamp helpers for Key Portal reporting."""

from datetime import datetime, timezone, timedelta

BEIJING_TZ = timezone(timedelta(hours=8))


def usage_date_from_timestamp(timestamp):
    text = str(timestamp or "").strip()
    if not text:
        return ""
    try:
        iso_text = text.replace("Z", "+00:00") if text.endswith("Z") else text
        parsed = datetime.fromisoformat(iso_text)
        if parsed.tzinfo is None:
            return parsed.strftime("%Y-%m-%d")
        return parsed.astimezone(BEIJING_TZ).strftime("%Y-%m-%d")
    except Exception:
        return text.split("T")[0] if "T" in text else text[:10]


def build_key_to_user_mapping(user_keys_data):
    """Build reverse mapping from API key to user email."""
    key_to_user = {}
    keys_info = (user_keys_data or {}).get("keys", {})
    for api_key, key_data in keys_info.items():
        email = (key_data or {}).get("email", "")
        if email:
            key_to_user[api_key] = email
    return key_to_user
