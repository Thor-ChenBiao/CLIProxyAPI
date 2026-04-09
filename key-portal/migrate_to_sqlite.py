#!/usr/bin/env python3
"""
Migrate usage data from CSV to SQLite database.
"""

import sqlite3
import csv
import json
import os
import sys
from datetime import datetime
from collections import defaultdict
import requests

# Configuration
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_FILE = os.path.join(BASE_DIR, "data", "usage.db")
CSV_FILE = os.path.join(BASE_DIR, "data", "usage_history.csv")
SCHEMA_FILE = os.path.join(BASE_DIR, "db_schema.sql")
USER_KEYS_FILE = os.path.join(BASE_DIR, "data", "user_keys.json")

MANAGEMENT_API_URL = "http://localhost:8317/v0/management/usage"
MANAGEMENT_API_KEY = "admin123"


def create_database():
    """Create database and tables."""
    print(f"[DB] Creating database at {DB_FILE}")

    os.makedirs(os.path.dirname(DB_FILE), exist_ok=True)

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()

    # Read and execute schema
    with open(SCHEMA_FILE, 'r') as f:
        schema_sql = f.read()

    cursor.executescript(schema_sql)
    conn.commit()

    print("[DB] Database created successfully")
    return conn


def migrate_csv_to_daily_usage(conn):
    """Migrate CSV data to daily_usage table."""
    print("[CSV] Migrating CSV data to database...")

    if not os.path.exists(CSV_FILE):
        print(f"[CSV] Warning: {CSV_FILE} not found, skipping CSV migration")
        return

    cursor = conn.cursor()
    imported = 0

    with open(CSV_FILE, 'r', newline='') as f:
        reader = csv.DictReader(f)
        for row in reader:
            date = row.get('date', '').strip()
            if not date:
                continue

            now = datetime.utcnow().isoformat() + 'Z'

            cursor.execute("""
                INSERT OR REPLACE INTO daily_usage
                (date, total_requests, success_count, failure_count,
                 total_tokens, input_tokens, output_tokens, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                date,
                int(row.get('total_requests', 0)),
                int(row.get('success_count', 0)),
                int(row.get('failure_count', 0)),
                int(row.get('total_tokens', 0)),
                int(row.get('input_tokens', 0)),
                int(row.get('output_tokens', 0)),
                now,
                now
            ))
            imported += 1

    conn.commit()
    print(f"[CSV] Imported {imported} daily records from CSV")


def load_user_keys_mapping():
    """Load user keys mapping from JSON."""
    if not os.path.exists(USER_KEYS_FILE):
        print(f"[UserKeys] Warning: {USER_KEYS_FILE} not found")
        return {}

    with open(USER_KEYS_FILE, 'r') as f:
        data = json.load(f)

    # Build reverse mapping: api_key -> email
    key_to_user = {}
    keys_info = data.get('keys', {})

    for api_key, key_data in keys_info.items():
        email = key_data.get('email', '')
        if email:
            key_to_user[api_key] = email

    print(f"[UserKeys] Loaded {len(key_to_user)} API key mappings")
    return key_to_user


def fetch_current_usage_from_api():
    """Fetch current usage data from Management API."""
    print("[API] Fetching usage data from Management API...")

    try:
        headers = {
            "Authorization": f"Bearer {MANAGEMENT_API_KEY}"
        }
        response = requests.get(MANAGEMENT_API_URL, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        print("[API] Successfully fetched usage data")
        return data
    except Exception as e:
        print(f"[API] Error fetching data: {e}")
        return None


def migrate_api_data_to_user_usage(conn, api_data, key_to_user):
    """Migrate API data to user_usage table."""
    print("[API] Migrating API data to user_usage table...")

    if not api_data:
        print("[API] No data to migrate")
        return

    cursor = conn.cursor()

    # Aggregate by date + user + api_key
    usage_map = defaultdict(lambda: {
        'total_requests': 0,
        'success_count': 0,
        'failure_count': 0,
        'total_tokens': 0,
        'input_tokens': 0,
        'output_tokens': 0,
    })

    usage = api_data.get('usage', {})
    apis = usage.get('apis', {})

    for api_key, api_info in apis.items():
        user_email = key_to_user.get(api_key, 'unknown')

        # Get request details
        models = api_info.get('models', {})
        for model_name, model_data in models.items():
            details = model_data.get('details', [])

            for detail in details:
                timestamp = detail.get('timestamp', '')
                if not timestamp:
                    continue

                # Extract date (YYYY-MM-DD)
                try:
                    date = timestamp.split('T')[0]
                except:
                    continue

                failed = detail.get('failed', False)
                tokens_info = detail.get('tokens', {})

                key = (date, user_email, api_key)

                usage_map[key]['total_requests'] += 1
                if failed:
                    usage_map[key]['failure_count'] += 1
                else:
                    usage_map[key]['success_count'] += 1

                usage_map[key]['total_tokens'] += tokens_info.get('total_tokens', 0)
                usage_map[key]['input_tokens'] += tokens_info.get('input_tokens', 0)
                usage_map[key]['output_tokens'] += tokens_info.get('output_tokens', 0)

    # Insert into database
    now = datetime.utcnow().isoformat() + 'Z'
    imported = 0

    for (date, user_email, api_key), stats in usage_map.items():
        cursor.execute("""
            INSERT OR REPLACE INTO user_usage
            (date, user_email, api_key, total_requests, success_count, failure_count,
             total_tokens, input_tokens, output_tokens, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            date,
            user_email,
            api_key,
            stats['total_requests'],
            stats['success_count'],
            stats['failure_count'],
            stats['total_tokens'],
            stats['input_tokens'],
            stats['output_tokens'],
            now,
            now
        ))
        imported += 1

    conn.commit()
    print(f"[API] Imported {imported} user usage records")


def verify_migration(conn):
    """Verify the migration results."""
    print("\n[Verify] Checking migration results...")

    cursor = conn.cursor()

    # Check daily_usage
    cursor.execute("SELECT COUNT(*), SUM(total_tokens), SUM(total_requests) FROM daily_usage")
    daily_count, daily_tokens, daily_requests = cursor.fetchone()
    print(f"[Verify] daily_usage: {daily_count} days, {daily_tokens:,} tokens, {daily_requests:,} requests")

    # Check user_usage
    cursor.execute("SELECT COUNT(*), SUM(total_tokens), SUM(total_requests) FROM user_usage")
    user_count, user_tokens, user_requests = cursor.fetchone()
    user_tokens = user_tokens or 0
    user_requests = user_requests or 0
    print(f"[Verify] user_usage: {user_count} records, {user_tokens:,} tokens, {user_requests:,} requests")

    # Check unique users
    cursor.execute("SELECT COUNT(DISTINCT user_email) FROM user_usage")
    unique_users = cursor.fetchone()[0]
    print(f"[Verify] Unique users: {unique_users}")

    # Sample data
    cursor.execute("""
        SELECT date, user_email, total_requests, total_tokens
        FROM user_usage
        ORDER BY total_tokens DESC
        LIMIT 5
    """)
    print("\n[Verify] Top 5 user usage records:")
    for row in cursor.fetchall():
        print(f"  {row[0]} | {row[1]} | {row[2]:,} requests | {row[3]:,} tokens")


def main():
    """Main migration function."""
    print("=" * 60)
    print("Key Portal - Migrate to SQLite Database")
    print("=" * 60)

    # Create database
    conn = create_database()

    # Migrate CSV data
    migrate_csv_to_daily_usage(conn)

    # Load user keys mapping
    key_to_user = load_user_keys_mapping()

    # Fetch and migrate API data
    api_data = fetch_current_usage_from_api()
    migrate_api_data_to_user_usage(conn, api_data, key_to_user)

    # Verify migration
    verify_migration(conn)

    conn.close()

    print("\n" + "=" * 60)
    print("Migration completed successfully!")
    print(f"Database location: {DB_FILE}")
    print("=" * 60)


if __name__ == "__main__":
    main()
