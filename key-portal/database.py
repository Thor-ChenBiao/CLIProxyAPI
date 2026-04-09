"""
Database module for Key Portal usage tracking.
"""

import sqlite3
import os
from datetime import datetime
from collections import defaultdict
from contextlib import contextmanager

DB_FILE = os.path.join(os.path.dirname(__file__), "data", "usage.db")


@contextmanager
def get_db_connection():
    """Get database connection context manager."""
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row  # Enable row access by column name
    try:
        yield conn
    finally:
        conn.close()


def init_database():
    """Initialize database if it doesn't exist."""
    if not os.path.exists(DB_FILE):
        print("[DB] Database not found, please run migrate_to_sqlite.py first")
        return False

    print(f"[DB] Using database: {DB_FILE}")
    return True


def upsert_daily_usage(date, total_requests, success_count, failure_count,
                       total_tokens, input_tokens, output_tokens):
    """Insert or update daily usage statistics."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat() + 'Z'

        cursor.execute("""
            INSERT OR REPLACE INTO daily_usage
            (date, total_requests, success_count, failure_count,
             total_tokens, input_tokens, output_tokens, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM daily_usage WHERE date = ?), ?),
                    ?)
        """, (
            date, total_requests, success_count, failure_count,
            total_tokens, input_tokens, output_tokens,
            date, now, now
        ))

        conn.commit()


def upsert_user_usage(date, user_email, api_key, total_requests,
                      success_count, failure_count, total_tokens,
                      input_tokens, output_tokens):
    """Insert or update user usage statistics."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        now = datetime.utcnow().isoformat() + 'Z'

        cursor.execute("""
            INSERT OR REPLACE INTO user_usage
            (date, user_email, api_key, total_requests, success_count, failure_count,
             total_tokens, input_tokens, output_tokens, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?,
                    COALESCE((SELECT created_at FROM user_usage WHERE date = ? AND user_email = ? AND api_key = ?), ?),
                    ?)
        """, (
            date, user_email, api_key, total_requests, success_count, failure_count,
            total_tokens, input_tokens, output_tokens,
            date, user_email, api_key, now, now
        ))

        conn.commit()


def get_daily_usage_history():
    """Get all daily usage records."""
    with get_db_connection() as conn:
        cursor = conn.cursor()
        cursor.execute("""
            SELECT date, total_requests, success_count, failure_count,
                   total_tokens, input_tokens, output_tokens
            FROM daily_usage
            ORDER BY date ASC
        """)

        results = []
        for row in cursor.fetchall():
            results.append({
                'date': row['date'],
                'total_requests': row['total_requests'],
                'success_count': row['success_count'],
                'failure_count': row['failure_count'],
                'total_tokens': row['total_tokens'],
                'input_tokens': row['input_tokens'],
                'output_tokens': row['output_tokens'],
            })

        return results


def get_user_usage_by_period(period='month'):
    """
    Get user usage aggregated by period.

    Args:
        period: 'day', 'month', or 'year'

    Returns:
        List of {user_email, period_key, total_requests, total_tokens, ...}
    """
    with get_db_connection() as conn:
        cursor = conn.cursor()

        # Determine period grouping
        if period == 'month':
            period_sql = "substr(date, 1, 7)"  # YYYY-MM
        elif period == 'year':
            period_sql = "substr(date, 1, 4)"  # YYYY
        else:  # day
            period_sql = "date"  # YYYY-MM-DD

        cursor.execute(f"""
            SELECT
                user_email,
                {period_sql} as period_key,
                SUM(total_requests) as total_requests,
                SUM(success_count) as success_count,
                SUM(failure_count) as failure_count,
                SUM(total_tokens) as total_tokens,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                GROUP_CONCAT(DISTINCT api_key) as api_keys
            FROM user_usage
            GROUP BY user_email, period_key
            ORDER BY period_key DESC, total_tokens DESC
        """)

        results = []
        for row in cursor.fetchall():
            results.append({
                'user_email': row['user_email'],
                'period': row['period_key'],
                'total_requests': row['total_requests'],
                'success_count': row['success_count'],
                'failure_count': row['failure_count'],
                'total_tokens': row['total_tokens'],
                'input_tokens': row['input_tokens'],
                'output_tokens': row['output_tokens'],
                'api_keys': row['api_keys'].split(',') if row['api_keys'] else [],
            })

        return results


def get_user_total_usage(user_email):
    """Get total usage for a specific user."""
    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                SUM(total_requests) as total_requests,
                SUM(success_count) as success_count,
                SUM(failure_count) as failure_count,
                SUM(total_tokens) as total_tokens,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                COUNT(DISTINCT api_key) as num_keys,
                COUNT(DISTINCT date) as num_days
            FROM user_usage
            WHERE user_email = ?
        """, (user_email,))

        row = cursor.fetchone()
        if not row:
            return None

        return {
            'user_email': user_email,
            'total_requests': row['total_requests'] or 0,
            'success_count': row['success_count'] or 0,
            'failure_count': row['failure_count'] or 0,
            'total_tokens': row['total_tokens'] or 0,
            'input_tokens': row['input_tokens'] or 0,
            'output_tokens': row['output_tokens'] or 0,
            'num_keys': row['num_keys'] or 0,
            'num_days': row['num_days'] or 0,
        }


def get_all_users_total_usage():
    """Get total usage for all users."""
    with get_db_connection() as conn:
        cursor = conn.cursor()

        cursor.execute("""
            SELECT
                user_email,
                SUM(total_requests) as total_requests,
                SUM(success_count) as success_count,
                SUM(failure_count) as failure_count,
                SUM(total_tokens) as total_tokens,
                SUM(input_tokens) as input_tokens,
                SUM(output_tokens) as output_tokens,
                COUNT(DISTINCT api_key) as num_keys,
                COUNT(DISTINCT date) as num_days
            FROM user_usage
            GROUP BY user_email
            ORDER BY total_tokens DESC
        """)

        results = []
        for row in cursor.fetchall():
            results.append({
                'user_email': row['user_email'],
                'total_requests': row['total_requests'] or 0,
                'success_count': row['success_count'] or 0,
                'failure_count': row['failure_count'] or 0,
                'total_tokens': row['total_tokens'] or 0,
                'input_tokens': row['input_tokens'] or 0,
                'output_tokens': row['output_tokens'] or 0,
                'num_keys': row['num_keys'] or 0,
                'num_days': row['num_days'] or 0,
            })

        return results


def get_usage_aggregated():
    """Get usage history with daily, monthly, and yearly aggregations."""
    history = get_daily_usage_history()

    by_month = defaultdict(lambda: {"total_tokens": 0, "total_requests": 0})
    by_year = defaultdict(lambda: {"total_tokens": 0, "total_requests": 0})

    for data in history:
        date = data['date']

        # Monthly aggregation (YYYY-MM)
        month_key = date[:7]
        by_month[month_key]["total_tokens"] += data["total_tokens"]
        by_month[month_key]["total_requests"] += data["total_requests"]

        # Yearly aggregation (YYYY)
        year_key = date[:4]
        by_year[year_key]["total_tokens"] += data["total_tokens"]
        by_year[year_key]["total_requests"] += data["total_requests"]

    return {
        "history": history,
        "by_month": dict(by_month),
        "by_year": dict(by_year)
    }
