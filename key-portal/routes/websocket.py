"""
WebSocket handlers for real-time usage updates.
"""


def register_websocket_handlers(socketio, broadcast_usage_update_func):
    """Register WebSocket event handlers."""

    @socketio.on("connect")
    def handle_connect():
        """Handle client connection."""
        print(f"[WebSocket] Client connected")
        # Send current usage immediately
        broadcast_usage_update_func()

    @socketio.on("disconnect")
    def handle_disconnect():
        """Handle client disconnection."""
        print(f"[WebSocket] Client disconnected")
