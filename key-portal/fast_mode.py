"""Shared Redis-backed control for LiteLLM Fast mode."""

import json
from datetime import datetime, timezone

try:
    import redis
except Exception:  # pragma: no cover - optional dependency
    redis = None


DEFAULT_REDIS_KEY = "cliproxy:fast-mode:enabled"
DEFAULT_METADATA_KEY = "cliproxy:fast-mode:metadata"
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


class FastModeUnavailable(RuntimeError):
    """Raised when the shared Fast mode state cannot be read or written."""


def parse_enabled(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    normalized = str(value or "").strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError("invalid Fast mode value")


class FastModeStore:
    def __init__(
        self,
        redis_url="",
        redis_key=DEFAULT_REDIS_KEY,
        metadata_key=DEFAULT_METADATA_KEY,
        default_enabled=True,
        redis_timeout_seconds=2.0,
        redis_client=None,
    ):
        self.redis_key = str(redis_key or DEFAULT_REDIS_KEY)
        self.metadata_key = str(metadata_key or DEFAULT_METADATA_KEY)
        self.default_enabled = bool(default_enabled)
        self._redis = redis_client
        if self._redis is None and redis_url and redis:
            try:
                self._redis = redis.Redis.from_url(
                    redis_url,
                    decode_responses=True,
                    socket_connect_timeout=float(redis_timeout_seconds),
                    socket_timeout=float(redis_timeout_seconds),
                )
            except Exception:
                self._redis = None

    def _client(self):
        if self._redis is None:
            raise FastModeUnavailable("Redis is not configured")
        return self._redis

    def load(self):
        client = self._client()
        try:
            raw_enabled = client.get(self.redis_key)
            raw_metadata = client.get(self.metadata_key)
        except Exception as exc:
            raise FastModeUnavailable("Redis read failed") from exc

        if raw_enabled is None:
            enabled = self.default_enabled
            source = "default"
        else:
            try:
                enabled = parse_enabled(raw_enabled)
            except ValueError as exc:
                raise FastModeUnavailable("Redis contains an invalid Fast mode value") from exc
            source = "redis"

        metadata = {}
        if raw_metadata:
            try:
                metadata = json.loads(raw_metadata)
            except (TypeError, ValueError):
                metadata = {}

        return {
            "enabled": enabled,
            "available": True,
            "source": source,
            "updated_at": str(metadata.get("updated_at") or ""),
            "updated_by": str(metadata.get("updated_by") or ""),
            "reason": str(metadata.get("reason") or ""),
        }

    def save(self, enabled, updated_by="", reason=""):
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        state = {
            "enabled": enabled,
            "available": True,
            "source": "redis",
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "updated_by": str(updated_by or ""),
            "reason": str(reason or ""),
        }
        client = self._client()
        try:
            pipeline = client.pipeline(transaction=True)
            pipeline.set(self.redis_key, "1" if enabled else "0")
            pipeline.set(
                self.metadata_key,
                json.dumps(state, ensure_ascii=False, separators=(",", ":")),
            )
            pipeline.execute()
        except Exception as exc:
            raise FastModeUnavailable("Redis write failed") from exc
        return state
