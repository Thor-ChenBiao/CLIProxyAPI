"""Subscribe to CLIProxyAPI usage broadcasts and persist minute aggregates."""

import json
import queue
import threading
import time
from collections import defaultdict
from urllib.parse import urlsplit

import redis


class AuthUsageCollector:
    def __init__(
        self,
        store,
        nodes,
        management_key,
        flush_seconds=1.0,
        queue_capacity=20000,
        batch_size=5000,
        redis_factory=None,
    ):
        self.store = store
        self.nodes = [dict(node) for node in nodes or []]
        self.management_key = management_key
        self.flush_seconds = max(0.2, float(flush_seconds or 1))
        self.batch_size = max(1, int(batch_size or 5000))
        self.redis_factory = redis_factory or redis.Redis
        self.records = queue.Queue(maxsize=max(100, int(queue_capacity or 20000)))
        self.stop_event = threading.Event()
        self.status_lock = threading.Lock()
        self.connected = {node.get("name", ""): False for node in self.nodes}
        self.node_names = list(self.connected)
        self.dropped_records = False
        self.threads = []
        self.started = False

    @staticmethod
    def connection_kwargs(node, management_key):
        parsed = urlsplit(str(node.get("url") or ""))
        use_tls = parsed.scheme.lower() == "https"
        kwargs = {
            "host": parsed.hostname or "127.0.0.1",
            "port": parsed.port or (443 if use_tls else 80),
            "password": management_key,
            "ssl": use_tls,
            "socket_connect_timeout": 10,
            "socket_timeout": 30,
            "socket_keepalive": True,
            "health_check_interval": 15,
            "decode_responses": False,
            "lib_name": None,
            "lib_version": None,
        }
        if use_tls:
            kwargs.update({"ssl_cert_reqs": "none", "ssl_check_hostname": False})
        return kwargs

    def set_node_connected(self, node_name, connected):
        with self.status_lock:
            self.connected[node_name] = bool(connected)

    def collection_complete(self):
        with self.status_lock:
            return bool(self.connected) and all(self.connected.values()) and not self.dropped_records

    @staticmethod
    def parse_message(payload):
        try:
            if isinstance(payload, bytes):
                payload = payload.decode("utf-8")
            record = json.loads(payload) if isinstance(payload, str) else payload
        except (UnicodeDecodeError, json.JSONDecodeError):
            return None
        if not isinstance(record, dict):
            return None
        if record.get("support_refresh") or record.get("refresh"):
            return None
        return record

    def enqueue_message(self, node_name, payload):
        record = self.parse_message(payload)
        if not record:
            return False
        try:
            self.records.put_nowait((node_name, record))
            return True
        except queue.Full:
            with self.status_lock:
                self.dropped_records = True
            return False

    def flush_once(self):
        grouped = defaultdict(list)
        for _ in range(self.batch_size):
            try:
                node_name, record = self.records.get_nowait()
            except queue.Empty:
                break
            grouped[node_name].append(record)
        results = [(node_name, records, None) for node_name, records in grouped.items()]
        complete = self.collection_complete()
        result = self.store.ingest_results(
            results,
            collection_complete=complete,
            coverage_nodes=self.node_names,
        )
        with self.status_lock:
            self.dropped_records = False
        return result

    def _flush_loop(self):
        while not self.stop_event.wait(self.flush_seconds):
            try:
                self.flush_once()
            except Exception as exc:
                print(f"[AuthUsage] Aggregate write failed: {exc}")

    def _subscribe_node(self, node):
        node_name = node.get("name") or "unknown"
        while not self.stop_event.is_set():
            client = None
            pubsub = None
            try:
                client = self.redis_factory(**self.connection_kwargs(node, self.management_key))
                pubsub = client.pubsub(ignore_subscribe_messages=True)
                pubsub.subscribe("usage")
                pubsub.get_message(ignore_subscribe_messages=True, timeout=5)
                self.set_node_connected(node_name, True)
                print(f"[AuthUsage] Subscribed to {node_name}")
                while not self.stop_event.is_set():
                    message = pubsub.get_message(ignore_subscribe_messages=True, timeout=1)
                    if message and message.get("type") == "message":
                        self.enqueue_message(node_name, message.get("data"))
            except Exception as exc:
                print(f"[AuthUsage] {node_name} subscription unavailable: {exc}")
            finally:
                self.set_node_connected(node_name, False)
                if pubsub is not None:
                    try:
                        pubsub.close()
                    except Exception:
                        pass
                if client is not None:
                    try:
                        client.close()
                    except Exception:
                        pass
            self.stop_event.wait(2)

    def start(self):
        if self.started:
            return False
        self.started = True
        for node in self.nodes:
            thread = threading.Thread(
                target=self._subscribe_node,
                args=(node,),
                name=f"auth-usage-{node.get('name') or 'unknown'}",
                daemon=True,
            )
            thread.start()
            self.threads.append(thread)
        writer = threading.Thread(target=self._flush_loop, name="auth-usage-writer", daemon=True)
        writer.start()
        self.threads.append(writer)
        return True

    def stop(self):
        self.stop_event.set()
        deadline = time.monotonic() + 5
        for thread in self.threads:
            thread.join(timeout=max(0, deadline - time.monotonic()))
