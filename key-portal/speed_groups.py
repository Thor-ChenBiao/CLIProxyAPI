STANDARD = "standard"
FAST = "fast"
ALLOWED = {STANDARD, FAST}


def normalize_speed_group(value):
    normalized = str(value or "").strip().lower()
    return normalized if normalized in ALLOWED else STANDARD


def require_speed_group(value):
    normalized = str(value or "").strip().lower()
    if normalized not in ALLOWED:
        raise ValueError("speed_group must be standard or fast")
    return normalized


def effective_speed_group(metadata):
    return normalize_speed_group((metadata or {}).get("speed_group"))


def canonical_litellm_key(api_key):
    value = str(api_key or "").strip()
    if value.startswith("usr_pool_"):
        return "sk-" + value
    return value


def merge_speed_group_metadata(metadata, speed_group):
    return {
        **(metadata or {}),
        "speed_group": require_speed_group(speed_group),
    }
