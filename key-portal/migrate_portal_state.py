#!/usr/bin/env python3
"""
Migrate Key Portal local state into the configured Postgres backend.

This script is safe to inspect without running. Run it only after setting
KEY_PORTAL_DATABASE_URL. It imports:
- data/user_keys.json
- data/key_pool.json
- approval_requests from data/usage.db
"""

import argparse
import json
import os
import sqlite3
from pathlib import Path

import config
import portal_state


BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
USER_KEYS_FILE = DATA_DIR / "user_keys.json"
KEY_POOL_FILE = DATA_DIR / "key_pool.json"
SQLITE_FILE = DATA_DIR / "usage.db"


def load_json(path, default):
    if not path.exists():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def import_approval_requests(dry_run=False):
    if not SQLITE_FILE.exists():
        return 0
    conn_sqlite = sqlite3.connect(f"file:{SQLITE_FILE}?mode=ro", uri=True)
    conn_sqlite.row_factory = sqlite3.Row
    try:
        rows = conn_sqlite.execute("SELECT * FROM approval_requests ORDER BY id").fetchall()
    except sqlite3.Error:
        rows = []
    finally:
        conn_sqlite.close()
    if dry_run or not rows:
        return len(rows)

    state = portal_state.current()
    if not state.ensure_schema():
        raise RuntimeError("Postgres backend is not enabled")
    with state.connect() as conn:
        with conn.cursor() as cur:
            for row in rows:
                payload_text = row["request_payload"] if "request_payload" in row.keys() else ""
                try:
                    payload = json.loads(payload_text or "{}")
                except Exception:
                    payload = {}
                cur.execute(
                    """
                    INSERT INTO key_portal_approval_requests (
                        id, email, name, label, model_group, reason, daily_budget,
                        instance_id, status, api_key, request_type, request_payload,
                        created_at, resolved_at, updated_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now())
                    ON CONFLICT (id) DO UPDATE SET
                        email = EXCLUDED.email,
                        name = EXCLUDED.name,
                        label = EXCLUDED.label,
                        model_group = EXCLUDED.model_group,
                        reason = EXCLUDED.reason,
                        daily_budget = EXCLUDED.daily_budget,
                        instance_id = EXCLUDED.instance_id,
                        status = EXCLUDED.status,
                        api_key = EXCLUDED.api_key,
                        request_type = EXCLUDED.request_type,
                        request_payload = EXCLUDED.request_payload,
                        resolved_at = EXCLUDED.resolved_at,
                        updated_at = now()
                    """,
                    (
                        row["id"],
                        row["email"],
                        row["name"],
                        row["label"],
                        row["model_group"],
                        row["reason"],
                        row["daily_budget"],
                        row["instance_id"],
                        row["status"],
                        row["api_key"],
                        row["request_type"] if "request_type" in row.keys() else "new_key",
                        portal_state.Jsonb(payload),
                        row["created_at"],
                        row["resolved_at"],
                    ),
                )
        conn.commit()
    return len(rows)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--dry-run", action="store_true", help="show what would be imported without writing")
    args = parser.parse_args()

    if not config.KEY_PORTAL_DATABASE_URL:
        raise SystemExit("A database URL is required")

    portal_state.configure(config.KEY_PORTAL_DATABASE_URL, config.KEY_PORTAL_REDIS_URL)
    state = portal_state.current()
    if not args.dry_run:
        state.ensure_schema()

    user_keys = load_json(USER_KEYS_FILE, {"version": "1.0", "users": {}, "keys": {}})
    key_pool = load_json(KEY_POOL_FILE, {"unused": [], "assigned": {}})
    approval_count = import_approval_requests(dry_run=args.dry_run)

    print(f"user_keys: users={len(user_keys.get('users', {}))} keys={len(user_keys.get('keys', {}))}")
    print(f"key_pool: unused={len(key_pool.get('unused', []))} assigned={len(key_pool.get('assigned', {}))}")
    print(f"approval_requests: rows={approval_count}")
    if args.dry_run:
        print("dry-run only; no user/key/pool writes performed")
        return

    if not portal_state.save_user_keys(user_keys):
        raise SystemExit("failed to save user_keys to Postgres")
    if not portal_state.save_key_pool(key_pool):
        raise SystemExit("failed to save key_pool to Postgres")
    print("migration completed")


if __name__ == "__main__":
    main()
