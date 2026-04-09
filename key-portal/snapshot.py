"""
Snapshot management for CLIProxyAPI restart recovery.
Handles exporting and importing usage statistics snapshots.
"""

import os
import json
from datetime import datetime


# Snapshot file path
SNAPSHOT_FILE = os.path.join(os.path.dirname(__file__), "data", "cliproxy_snapshot.json")

# State for restart detection
_cliproxy_state = {
    "last_total_tokens": 0,
    "last_total_requests": 0,
    "last_check_time": None,
    "restart_count": 0
}


def export_cliproxy_snapshot(call_management_api):
    """Export complete usage snapshot from CLIProxyAPI."""
    try:
        print(f"[Snapshot] Exporting usage data from CLIProxyAPI...")
        data, err = call_management_api("GET", "/v0/management/usage/export")

        if err:
            print(f"[Snapshot] Export failed: {err}")
            return False

        # Save to file
        os.makedirs(os.path.dirname(SNAPSHOT_FILE), exist_ok=True)
        with open(SNAPSHOT_FILE, "w") as f:
            json.dump(data, f, indent=2)

        usage = data.get("usage", {})
        total_tokens = usage.get("total_tokens", 0)
        total_requests = usage.get("total_requests", 0)

        print(f"[Snapshot] ✅ Exported: {total_tokens:,} tokens, {total_requests:,} requests")
        print(f"[Snapshot] Saved to: {SNAPSHOT_FILE}")
        return True

    except Exception as e:
        print(f"[Snapshot] Export error: {e}")
        import traceback
        traceback.print_exc()
        return False


def import_cliproxy_snapshot(call_management_api):
    """Import previously exported snapshot into CLIProxyAPI."""
    try:
        if not os.path.exists(SNAPSHOT_FILE):
            print(f"[Snapshot] No snapshot file found at {SNAPSHOT_FILE}")
            return False

        print(f"[Snapshot] Loading snapshot from {SNAPSHOT_FILE}...")
        with open(SNAPSHOT_FILE, "r") as f:
            snapshot = json.load(f)

        exported_at = snapshot.get("exported_at", "unknown")
        usage = snapshot.get("usage", {})
        total_tokens = usage.get("total_tokens", 0)
        total_requests = usage.get("total_requests", 0)

        print(f"[Snapshot] Snapshot info:")
        print(f"  Exported at: {exported_at}")
        print(f"  Tokens:      {total_tokens:,}")
        print(f"  Requests:    {total_requests:,}")

        print(f"[Snapshot] Importing into CLIProxyAPI...")
        data, err = call_management_api("POST", "/v0/management/usage/import", snapshot)

        if err:
            print(f"[Snapshot] Import failed: {err}")
            return False

        added = data.get("added", 0)
        skipped = data.get("skipped", 0)
        new_total_requests = data.get("total_requests", 0)

        print(f"[Snapshot] ✅ Import completed:")
        print(f"  Added:          {added:,} records")
        print(f"  Skipped:        {skipped:,} records")
        print(f"  Total requests: {new_total_requests:,}")

        return True

    except Exception as e:
        print(f"[Snapshot] Import error: {e}")
        import traceback
        traceback.print_exc()
        return False


def detect_cliproxy_restart(current_tokens, current_requests):
    """
    Detect if CLIProxyAPI has restarted by checking if token count decreased.
    In-memory statistics only increase, so any decrease indicates a restart.
    """
    now = datetime.now()

    # First check, initialize state
    if _cliproxy_state["last_check_time"] is None:
        _cliproxy_state["last_total_tokens"] = current_tokens
        _cliproxy_state["last_total_requests"] = current_requests
        _cliproxy_state["last_check_time"] = now
        print(f"[Restart] Monitoring initialized: {current_tokens:,} tokens, {current_requests:,} requests")
        return False

    # Check if data decreased (restart indicator)
    tokens_decreased = current_tokens < _cliproxy_state["last_total_tokens"]
    requests_decreased = current_requests < _cliproxy_state["last_total_requests"]

    if tokens_decreased or requests_decreased:
        _cliproxy_state["restart_count"] += 1

        print()
        print("=" * 80)
        print(f"🔄 CLIProxyAPI RESTART DETECTED! (Restart #{_cliproxy_state['restart_count']})")
        print("=" * 80)
        print(f"Previous state (before restart):")
        print(f"  Tokens:   {_cliproxy_state['last_total_tokens']:,}")
        print(f"  Requests: {_cliproxy_state['last_total_requests']:,}")
        print(f"  Time:     {_cliproxy_state['last_check_time']}")
        print()
        print(f"Current state (after restart):")
        print(f"  Tokens:   {current_tokens:,}")
        print(f"  Requests: {current_requests:,}")
        print(f"  Time:     {now}")
        print()
        print(f"Data loss (in-memory):")
        print(f"  Tokens:   {_cliproxy_state['last_total_tokens'] - current_tokens:,}")
        print(f"  Requests: {_cliproxy_state['last_total_requests'] - current_requests:,}")
        print("=" * 80)
        print()

        # Update state
        _cliproxy_state["last_total_tokens"] = current_tokens
        _cliproxy_state["last_total_requests"] = current_requests
        _cliproxy_state["last_check_time"] = now

        return True

    # Normal growth, update state
    _cliproxy_state["last_total_tokens"] = current_tokens
    _cliproxy_state["last_total_requests"] = current_requests
    _cliproxy_state["last_check_time"] = now

    return False
