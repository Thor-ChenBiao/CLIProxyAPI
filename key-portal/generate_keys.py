#!/usr/bin/env python3
"""Generate API keys and save them to the Key Portal Postgres key pool."""

import os
import sys
import uuid

import requests

import config
import portal_state

KEYS_COUNT = int(os.environ.get("KEY_POOL_GENERATE_COUNT", "500") or "500")
KEY_PREFIX = os.environ.get("KEY_POOL_PREFIX", "usr_pool")
CLIPROXY_API_URL = os.environ.get("CLIPROXY_API_URL", "http://localhost:8317").rstrip("/")
CLIPROXY_MANAGEMENT_KEY = os.environ.get("CLIPROXY_MANAGEMENT_KEY", "admin123")


def generate_keys(count=500):
    """Generate unique API keys."""
    keys = []
    for i in range(count):
        short_id = str(uuid.uuid4()).replace("-", "")[:12]
        keys.append(f"{KEY_PREFIX}_{i + 1:04d}_{short_id}")
    return keys


def save_key_pool(keys):
    """Append generated keys to the Postgres-backed key pool."""
    portal_state.configure(config.KEY_PORTAL_DATABASE_URL, config.KEY_PORTAL_REDIS_URL)
    if not portal_state.ensure_schema():
        raise RuntimeError("KEY_PORTAL_DATABASE_URL is required to save generated keys")

    pool = portal_state.load_key_pool() or {"unused": [], "assigned": {}}
    existing = set(pool.get("unused", [])) | set((pool.get("assigned", {}) or {}).keys())
    new_keys = [key for key in keys if key not in existing]
    pool.setdefault("unused", []).extend(new_keys)
    if not portal_state.save_key_pool(pool):
        raise RuntimeError("failed to save generated keys to Postgres")
    print(f"✅ Saved {len(new_keys)} new keys to Postgres key pool")
    return new_keys


def add_keys_to_cliproxy(keys):
    """Add keys to CLIProxyAPI via Management API."""
    url = f"{CLIPROXY_API_URL}/v0/management/api-keys"
    headers = {
        "X-Management-Key": CLIPROXY_MANAGEMENT_KEY,
        "Content-Type": "application/json",
    }

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            existing_keys = resp.json().get("api_keys", [])
            print(f"📋 Found {len(existing_keys)} existing keys")
        else:
            existing_keys = []
            print("⚠️  No existing keys found")

        all_keys = list(dict.fromkeys(existing_keys + keys))
        resp = requests.put(url, headers=headers, json=all_keys, timeout=30)
        if resp.status_code == 200:
            print(f"✅ Added {len(keys)} keys to CLIProxyAPI")
            print(f"📊 Total keys in CLIProxyAPI: {len(all_keys)}")
            return True
        print(f"❌ Failed to add keys: {resp.status_code} - {resp.text}")
        return False
    except Exception as exc:
        print(f"❌ Error: {exc}")
        return False


def main():
    print("=" * 60)
    print("🔑 API Key Pool Generator")
    print("=" * 60)

    print(f"\n📝 Generating {KEYS_COUNT} API keys...")
    keys = generate_keys(KEYS_COUNT)
    print(f"✅ Generated {len(keys)} keys")

    print("\n💾 Saving generated keys to Postgres key pool...")
    new_keys = save_key_pool(keys)

    print("\n🚀 Adding keys to CLIProxyAPI...")
    success = add_keys_to_cliproxy(new_keys)
    if success:
        print("\n" + "=" * 60)
        print("✅ All done! Keys are ready to use.")
        print("=" * 60)
        print("\n📊 Summary:")
        print(f"  - Total keys generated: {len(keys)}")
        print(f"  - New keys saved: {len(new_keys)}")
        print(f"  - Key format: {KEY_PREFIX}_XXXX_XXXXXXXXXXXX")
        return 0
    print("\n❌ Failed to add keys to CLIProxyAPI")
    return 1


if __name__ == "__main__":
    sys.exit(main())
