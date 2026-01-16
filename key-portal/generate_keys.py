#!/usr/bin/env python3
"""
Key Pool Generator
Generate a batch of API keys and add them to CLIProxyAPI
"""

import json
import os
import uuid
import requests
from datetime import datetime

# Configuration
KEYS_COUNT = 500
KEY_PREFIX = "usr_pool"
DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
KEY_POOL_FILE = os.path.join(DATA_DIR, "key_pool.json")

# CLIProxyAPI config
CLIPROXY_API_URL = "http://localhost:8317"
CLIPROXY_MANAGEMENT_KEY = "admin123"


def generate_keys(count=500):
    """Generate unique API keys"""
    keys = []
    for i in range(count):
        short_id = str(uuid.uuid4()).replace('-', '')[:12]
        key = f"{KEY_PREFIX}_{i+1:04d}_{short_id}"
        keys.append(key)
    return keys


def save_key_pool(keys):
    """Save keys to key_pool.json"""
    os.makedirs(DATA_DIR, exist_ok=True)

    data = {
        "version": "1.0",
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "total": len(keys),
        "unused": keys,
        "assigned": {}
    }

    with open(KEY_POOL_FILE, "w") as f:
        json.dump(data, f, indent=2)

    print(f"✅ Saved {len(keys)} keys to {KEY_POOL_FILE}")


def add_keys_to_cliproxy(keys):
    """Add keys to CLIProxyAPI via Management API"""
    url = f"{CLIPROXY_API_URL}/v0/management/api-keys"
    headers = {
        "X-Management-Key": CLIPROXY_MANAGEMENT_KEY,
        "Content-Type": "application/json"
    }

    try:
        # Get existing keys
        resp = requests.get(url, headers=headers, timeout=30)
        if resp.status_code == 200:
            existing_keys = resp.json().get("api_keys", [])
            print(f"📋 Found {len(existing_keys)} existing keys")
        else:
            existing_keys = []
            print("⚠️  No existing keys found")

        # Merge with new keys
        all_keys = existing_keys + keys

        # Update
        resp = requests.put(url, headers=headers, json=all_keys, timeout=30)

        if resp.status_code == 200:
            print(f"✅ Added {len(keys)} keys to CLIProxyAPI")
            print(f"📊 Total keys in CLIProxyAPI: {len(all_keys)}")
            return True
        else:
            print(f"❌ Failed to add keys: {resp.status_code} - {resp.text}")
            return False

    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def main():
    print("=" * 60)
    print("🔑 API Key Pool Generator")
    print("=" * 60)

    # Generate keys
    print(f"\n📝 Generating {KEYS_COUNT} API keys...")
    keys = generate_keys(KEYS_COUNT)
    print(f"✅ Generated {len(keys)} keys")

    # Save to file
    print(f"\n💾 Saving to {KEY_POOL_FILE}...")
    save_key_pool(keys)

    # Add to CLIProxyAPI
    print(f"\n🚀 Adding keys to CLIProxyAPI...")
    success = add_keys_to_cliproxy(keys)

    if success:
        print("\n" + "=" * 60)
        print("✅ All done! Keys are ready to use.")
        print("=" * 60)
        print(f"\n📊 Summary:")
        print(f"  - Total keys generated: {len(keys)}")
        print(f"  - Key format: {KEY_PREFIX}_XXXX_XXXXXXXXXXXX")
        print(f"  - Storage: {KEY_POOL_FILE}")
        print(f"\n🎯 Next steps:")
        print(f"  1. Start Key Portal: python app.py")
        print(f"  2. Users can register at: http://localhost:8080/register")
    else:
        print("\n❌ Failed to add keys to CLIProxyAPI")
        print("Please check CLIProxyAPI is running and management key is correct")


if __name__ == "__main__":
    main()
