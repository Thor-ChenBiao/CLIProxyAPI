import os
import threading
import time

from litellm.integrations.custom_logger import CustomLogger

try:
    import redis
except Exception:  # pragma: no cover - LiteLLM deployments include redis
    redis = None


FAST_MODE_REDIS_KEY = os.environ.get(
    "FAST_MODE_REDIS_KEY", "cliproxy:fast-mode:enabled"
).strip() or "cliproxy:fast-mode:enabled"
TRUE_VALUES = {"1", "true", "yes", "on", "enabled"}
FALSE_VALUES = {"0", "false", "no", "off", "disabled"}


def env_bool(name, default):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in TRUE_VALUES


def env_float(name, default):
    try:
        return float(os.environ.get(name, "") or default)
    except (TypeError, ValueError):
        return default


def parse_enabled(value):
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="replace")
    normalized = str(value or "").strip().lower()
    if normalized in TRUE_VALUES:
        return True
    if normalized in FALSE_VALUES:
        return False
    raise ValueError("invalid Fast mode value")


def redis_client_from_environment():
    redis_url = os.environ.get("REDIS_URL", "").strip()
    if not redis_url or redis is None:
        return None
    timeout_seconds = max(0.1, env_float("FAST_MODE_REDIS_TIMEOUT_SECONDS", 0.5))
    try:
        return redis.Redis.from_url(
            redis_url,
            decode_responses=True,
            socket_connect_timeout=timeout_seconds,
            socket_timeout=timeout_seconds,
        )
    except Exception:
        return None


class SpeedGroupCallback(CustomLogger):
    def __init__(
        self,
        redis_client=None,
        cache_seconds=None,
        clock=None,
        missing_default=None,
        error_default=None,
    ):
        super().__init__()
        self.redis_client = (
            redis_client
            if redis_client is not None
            else redis_client_from_environment()
        )
        self.cache_seconds = max(
            0.0,
            env_float("FAST_MODE_CACHE_SECONDS", 5.0)
            if cache_seconds is None
            else float(cache_seconds),
        )
        self.clock = clock or time.monotonic
        self.missing_default = (
            env_bool("FAST_MODE_MISSING_DEFAULT_ENABLED", True)
            if missing_default is None
            else bool(missing_default)
        )
        self.error_default = (
            env_bool("FAST_MODE_ERROR_DEFAULT_ENABLED", False)
            if error_default is None
            else bool(error_default)
        )
        self._cached_enabled = None
        self._cached_at = None
        self._cache_lock = threading.Lock()

    def global_fast_enabled(self):
        now = self.clock()
        with self._cache_lock:
            if (
                self._cached_at is not None
                and now - self._cached_at < self.cache_seconds
            ):
                return self._cached_enabled

            try:
                if self.redis_client is None:
                    raise RuntimeError("Redis is not configured")
                raw_enabled = self.redis_client.get(FAST_MODE_REDIS_KEY)
                enabled = (
                    self.missing_default
                    if raw_enabled is None
                    else parse_enabled(raw_enabled)
                )
            except Exception:
                if self._cached_enabled is not None:
                    self._cached_at = now
                    return self._cached_enabled
                self._cached_enabled = self.error_default
                self._cached_at = now
                return self._cached_enabled

            self._cached_enabled = enabled
            self._cached_at = now
            return enabled

    async def async_pre_call_hook(
        self,
        user_api_key_dict,
        cache,
        data,
        call_type,
    ):
        request_data = dict(data or {})
        metadata = getattr(user_api_key_dict, "metadata", None) or {}
        speed_group = str(metadata.get("speed_group") or "").strip().lower()
        request_data["service_tier"] = (
            "priority"
            if self.global_fast_enabled() and speed_group == "fast"
            else "default"
        )
        return request_data


speed_group_callback = SpeedGroupCallback()
