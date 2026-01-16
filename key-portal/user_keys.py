"""
User Keys Management Module.
Handles user key assignment, revocation, and pool management.
"""

import os
import json
from datetime import datetime


# File paths
KEY_POOL_FILE = os.path.join(os.path.dirname(__file__), "data", "key_pool.json")
USER_KEYS_FILE = os.path.join(os.path.dirname(__file__), "data", "user_keys.json")

# Memory cache for user keys
_user_keys_cache = {
    "data": None,
    "loaded": False
}


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
            json.dump(data, f, indent=2, ensure_ascii=False)
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


def assign_key_to_user(email, name, label):
    """Assign an unused key from pool to user."""
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
    user_keys["users"][email]["api_keys"].append(api_key)

    # Add to keys index
    user_keys["keys"][api_key] = {
        "email": email,
        "label": label or "默认",
        "created_at": datetime.utcnow().isoformat() + "Z"
    }

    # Save
    save_user_keys(user_keys)

    print(f"[UserKeys] Assigned {api_key} to {email}")
    return api_key, None


def revoke_key(api_key, call_management_api):
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


def reload_user_keys_cache():
    """Force reload user keys cache."""
    _user_keys_cache["loaded"] = False
    return load_user_keys()
