"""Socket.IO usage summary broadcasting for Key Portal."""

import threading
from datetime import datetime


class UsageRealtimeBroadcaster:
    def __init__(self, socketio, usage_summary_loader):
        self.socketio = socketio
        self.usage_summary_loader = usage_summary_loader
        self.last_state = {"total_tokens": 0, "total_requests": 0}
        self.client_count = 0
        self.lock = threading.Lock()

    def has_clients(self):
        with self.lock:
            return self.client_count > 0

    def handle_connect(self):
        with self.lock:
            self.client_count += 1
            client_count = self.client_count
        print(f"[WebSocket] Client connected ({client_count} active)")
        self.broadcast(force=True)

    def handle_disconnect(self):
        with self.lock:
            self.client_count = max(0, self.client_count - 1)
            client_count = self.client_count
        print(f"[WebSocket] Client disconnected ({client_count} active)")

    def broadcast(self, force=False):
        if not force and not self.has_clients():
            return
        try:
            data, err = self.usage_summary_loader()
            if err:
                return

            current_tokens = data.get("total_tokens", 0)
            current_requests = data.get("total_requests", 0)
            if not force and current_tokens == self.last_state["total_tokens"] and current_requests == self.last_state["total_requests"]:
                return

            self.last_state["total_tokens"] = current_tokens
            self.last_state["total_requests"] = current_requests
            self.socketio.emit("usage_update", {
                "total_tokens": current_tokens,
                "total_requests": current_requests,
                "today_tokens": data.get("today_tokens", 0),
                "today_requests": data.get("today_requests", 0),
                "success_count": data.get("success_count", 0),
                "failure_count": data.get("failure_count", 0),
                "token_breakdown": data.get("token_breakdown", {}),
                "today": data.get("today", ""),
                "timestamp": datetime.now().isoformat(),
            })
            print(f"[WebSocket] Broadcast usage update: {current_tokens:,} tokens")
        except Exception as e:
            print(f"[WebSocket] Error broadcasting: {e}")
            import traceback
            traceback.print_exc()
