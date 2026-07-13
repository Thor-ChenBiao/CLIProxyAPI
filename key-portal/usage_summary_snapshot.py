"""Thread-safe stale-while-revalidate cache for dashboard usage summaries."""

import threading
import time


class UsageSummarySnapshot:
    def __init__(self, ttl_seconds=2, clock=None, thread_factory=None, cold_wait_seconds=10):
        self.ttl_seconds = max(0.1, float(ttl_seconds))
        self.clock = clock or time.monotonic
        self.thread_factory = thread_factory or self._new_thread
        self.cold_wait_seconds = max(0.1, float(cold_wait_seconds))
        self.condition = threading.Condition()
        self.data = None
        self.last_update = 0.0
        self.refreshing = False
        self.last_error = ""

    @staticmethod
    def _new_thread(target):
        return threading.Thread(target=target, daemon=True)

    def _response_locked(self, now):
        if self.data is None:
            return None
        age = max(0.0, now - self.last_update)
        response = dict(self.data)
        response["cache_age_seconds"] = round(age, 3)
        response["cache_refreshing"] = self.refreshing
        response["cache_stale"] = age >= self.ttl_seconds
        if self.last_error:
            response["cache_refresh_error"] = self.last_error
        else:
            response.pop("cache_refresh_error", None)
        return response

    def _refresh(self, loader):
        loaded = None
        error = ""
        try:
            loaded = loader()
            if not isinstance(loaded, dict) or not loaded:
                raise RuntimeError("usage summary refresh returned no data")
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__

        with self.condition:
            if loaded:
                self.data = dict(loaded)
                self.last_update = self.clock()
                self.last_error = ""
            else:
                self.last_error = error
            self.refreshing = False
            self.condition.notify_all()
        return bool(loaded)

    def refresh(self, loader):
        with self.condition:
            if self.refreshing:
                return False
            self.refreshing = True
        return self._refresh(loader)

    def get(self, loader):
        now = self.clock()
        start_background = False
        cold_load = False

        with self.condition:
            response = self._response_locked(now)
            if response is not None and not response["cache_stale"]:
                return response, None

            if response is not None:
                if not self.refreshing:
                    self.refreshing = True
                    start_background = True
                response["cache_refreshing"] = True
            elif self.refreshing:
                self.condition.wait_for(
                    lambda: self.data is not None or not self.refreshing,
                    timeout=self.cold_wait_seconds,
                )
                response = self._response_locked(self.clock())
                if response is not None:
                    return response, None
                return None, self.last_error or "usage summary refresh timed out"
            else:
                self.refreshing = True
                cold_load = True

        if start_background:
            self.thread_factory(lambda: self._refresh(loader)).start()
            return response, None

        if response is not None:
            return response, None

        if cold_load:
            self._refresh(loader)
            with self.condition:
                response = self._response_locked(self.clock())
                if response is not None:
                    return response, None
                return None, self.last_error or "usage summary unavailable"

        return None, "usage summary unavailable"
