"""
Usage data synchronization module.
Handles syncing usage data from CLIProxyAPI Management API to database.
"""

from collections import defaultdict
import database as db


def sync_usage_to_database(api_data, key_to_user_mapping):
    """
    Sync usage data from API response to database.

    Args:
        api_data: Dict from Management API /v0/management/usage
        key_to_user_mapping: Dict mapping api_key -> user_email

    Returns:
        Tuple (success: bool, stats: dict)
    """
    try:
        usage = api_data.get("usage", {})
        tokens_by_day = usage.get("tokens_by_day", {})
        requests_by_day = usage.get("requests_by_day", {})
        apis = usage.get("apis", {})

        # 1. Sync daily totals to database
        for date in set(list(tokens_by_day.keys()) + list(requests_by_day.keys())):
            db.upsert_daily_usage(
                date=date,
                total_requests=requests_by_day.get(date, 0),
                success_count=requests_by_day.get(date, 0),  # Approximate
                failure_count=0,  # Not available in aggregated data
                total_tokens=tokens_by_day.get(date, 0),
                input_tokens=0,
                output_tokens=0
            )

        # 2. Aggregate user usage by date + user + api_key
        usage_map = defaultdict(lambda: {
            'total_requests': 0,
            'success_count': 0,
            'failure_count': 0,
            'total_tokens': 0,
            'input_tokens': 0,
            'output_tokens': 0,
        })

        # Track unknown keys for logging
        unknown_keys_info = {}

        for api_key, api_info in apis.items():
            user_email = key_to_user_mapping.get(api_key, 'unknown')

            # Log if key is not mapped
            if user_email == 'unknown':
                if api_key not in unknown_keys_info:
                    unknown_keys_info[api_key] = {
                        'total_tokens': api_info.get('total_tokens', 0),
                        'total_requests': api_info.get('total_requests', 0)
                    }

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

        # 3. Insert into database
        for (date, user_email, api_key), stats in usage_map.items():
            db.upsert_user_usage(
                date=date,
                user_email=user_email,
                api_key=api_key,
                total_requests=stats['total_requests'],
                success_count=stats['success_count'],
                failure_count=stats['failure_count'],
                total_tokens=stats['total_tokens'],
                input_tokens=stats['input_tokens'],
                output_tokens=stats['output_tokens']
            )

        # 4. Log unknown keys
        if unknown_keys_info:
            print(f"[UsageSync] WARNING: Found {len(unknown_keys_info)} unknown API keys not in user_keys.json:")
            for key, info in unknown_keys_info.items():
                print(f"  - Key: {key}")
                print(f"    Tokens: {info['total_tokens']:,} | Requests: {info['total_requests']}")
            print(f"[UsageSync] These keys will be tracked as 'unknown' user. Consider adding them to user_keys.json")

        return True, {
            'user_records': len(usage_map),
            'daily_records': len(tokens_by_day),
            'total_tokens': sum(tokens_by_day.values()),
            'total_requests': sum(requests_by_day.values()),
            'unknown_keys': len(unknown_keys_info),
        }

    except Exception as e:
        import traceback
        traceback.print_exc()
        return False, {'error': str(e)}


def build_key_to_user_mapping(user_keys_data):
    """
    Build reverse mapping from API key to user email.

    Args:
        user_keys_data: Dict from user_keys.json

    Returns:
        Dict mapping api_key -> user_email
    """
    key_to_user = {}
    keys_info = user_keys_data.get('keys', {})

    for api_key, key_data in keys_info.items():
        email = key_data.get('email', '')
        if email:
            key_to_user[api_key] = email

    return key_to_user
