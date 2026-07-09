"""
State backends for Key Portal.

Postgres stores durable user/key/session facts. Redis stores OAuth state and
other explicitly transient coordination data.
"""

import hashlib
import json
import secrets
import threading
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

try:
    import psycopg
    from psycopg.rows import dict_row
    from psycopg.types.json import Jsonb
except Exception:  # pragma: no cover - optional dependency
    psycopg = None
    dict_row = None
    Jsonb = None

try:
    import redis
except Exception:  # pragma: no cover - optional dependency
    redis = None


UTC = timezone.utc
_state = None
_oauth_state_memory = {}
_oauth_state_lock = threading.Lock()
_session_memory = {}
_session_lock = threading.Lock()
_status_events_memory = {}
_status_events_lock = threading.Lock()
_status_event_next_id = 1


def configure(database_url="", redis_url=""):
    global _state
    _state = PortalState(database_url=database_url, redis_url=redis_url)
    return _state


def current():
    global _state
    if _state is None:
        _state = PortalState()
    return _state


def psycopg_database_url(value):
    text = (value or "").strip()
    if not text:
        return ""
    try:
        parts = urlsplit(text)
    except Exception:
        return text
    if not parts.query:
        return text
    filtered = [(key, val) for key, val in parse_qsl(parts.query, keep_blank_values=True) if key.lower() != "pgbouncer"]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(filtered), parts.fragment))


def is_pg_enabled():
    return current().pg_enabled


def is_redis_enabled():
    return current().redis_enabled


def ensure_schema():
    return current().ensure_schema()


def load_user_keys():
    return current().load_user_keys()


def save_user_keys(data):
    return current().save_user_keys(data)


def load_key_pool():
    return current().load_key_pool()


def save_key_pool(data):
    return current().save_key_pool(data)


def create_session(email, user_data, ttl_seconds=30 * 24 * 3600, request_meta=None):
    return current().create_session(email, user_data, ttl_seconds, request_meta or {})


def get_session(session_id):
    return current().get_session(session_id)


def destroy_session(session_id):
    return current().destroy_session(session_id)


def create_oauth_state(payload, ttl_seconds=10 * 60):
    return current().create_oauth_state(payload, ttl_seconds)


def consume_oauth_state(state):
    return current().consume_oauth_state(state)


def cache_get_json(key):
    return current().cache_get_json(key)


def cache_set_json(key, value, ttl_seconds=60):
    return current().cache_set_json(key, value, ttl_seconds)


def cache_delete(key):
    return current().cache_delete(key)


def upsert_status_event(**kwargs):
    return current().upsert_status_event(**kwargs)


def resolve_status_event(dedupe_key, **kwargs):
    return current().resolve_status_event(dedupe_key, **kwargs)


def mark_status_event_notified(event_id):
    return current().mark_status_event_notified(event_id)


def list_status_events(limit=100):
    return current().list_status_events(limit)


def update_status_event_note(event_id, admin_note):
    return current().update_status_event_note(event_id, admin_note)


def _now():
    return datetime.now(UTC)


def _json_value(value):
    if isinstance(value, str):
        try:
            return json.loads(value)
        except Exception:
            return {}
    return value or {}


def _iso(value):
    if not value:
        return ""
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat().replace("+00:00", "Z")
    return str(value)


def _session_hash(session_id):
    return hashlib.sha256(str(session_id or "").encode()).hexdigest()


def _public_session_payload(row):
    if not row:
        return None
    if row.get("expires_at") and row["expires_at"] <= _now():
        return None
    if row.get("revoked_at"):
        return None
    data = _json_value(row.get("user_data"))
    data.setdefault("email", row.get("email", ""))
    return {
        "email": row.get("email", ""),
        "user": data,
        "expires_at": _iso(row.get("expires_at")),
        "created_at": _iso(row.get("created_at")),
    }


def _status_event_payload(row):
    if not row:
        return None
    return {
        "id": row.get("id"),
        "event_type": row.get("event_type") or "",
        "dedupe_key": row.get("dedupe_key") or "",
        "status": row.get("status") or "",
        "severity": row.get("severity") or "info",
        "title": row.get("title") or "",
        "summary": row.get("summary") or "",
        "reason": row.get("reason") or "",
        "affected_nodes": _json_value(row.get("affected_nodes")) or [],
        "metadata": _json_value(row.get("metadata")),
        "admin_note": row.get("admin_note") or "",
        "started_at": _iso(row.get("started_at")),
        "resolved_at": _iso(row.get("resolved_at")),
        "last_seen_at": _iso(row.get("last_seen_at")),
        "notified_at": _iso(row.get("notified_at")),
        "created_at": _iso(row.get("created_at")),
        "updated_at": _iso(row.get("updated_at")),
    }


def _memory_status_event_payload(item):
    return _status_event_payload(dict(item or {}))


class PortalState:
    def __init__(self, database_url="", redis_url=""):
        self.database_url = psycopg_database_url(database_url)
        self.redis_url = (redis_url or "").strip()
        self.pg_enabled = bool(self.database_url and psycopg)
        self.redis_enabled = False
        self._redis = None
        self._schema_ready = False
        self._schema_lock = threading.Lock()

        if self.database_url and not psycopg:
            print("[PortalState] Database URL set but psycopg is not installed; using file fallback")

        if self.redis_url and redis:
            try:
                self._redis = redis.Redis.from_url(self.redis_url, decode_responses=True)
                self.redis_enabled = True
            except Exception as exc:
                print(f"[PortalState] Redis disabled: {exc}")
        elif self.redis_url and not redis:
            print("[PortalState] Redis URL set but redis package is not installed; Redis cache disabled")

    def connect(self):
        if not self.pg_enabled:
            return None
        return psycopg.connect(self.database_url, row_factory=dict_row)

    def ensure_schema(self):
        if not self.pg_enabled:
            return False
        with self._schema_lock:
            if self._schema_ready:
                return True
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_portal_users (
                            email text PRIMARY KEY,
                            name text NOT NULL DEFAULT '',
                            feishu_open_id text,
                            feishu_user_id text,
                            feishu_union_id text,
                            avatar_url text,
                            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            updated_at timestamptz NOT NULL DEFAULT now(),
                            last_login_at timestamptz
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_portal_api_keys (
                            api_key text PRIMARY KEY,
                            email text NOT NULL REFERENCES key_portal_users(email) ON UPDATE CASCADE,
                            label text NOT NULL DEFAULT '',
                            model_group text NOT NULL DEFAULT 'common',
                            source text NOT NULL DEFAULT 'cliproxy',
                            status text NOT NULL DEFAULT 'active',
                            max_budget double precision,
                            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            updated_at timestamptz NOT NULL DEFAULT now(),
                            revoked_at timestamptz
                        )
                        """
                    )
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_key_portal_api_keys_email ON key_portal_api_keys(email)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_key_portal_api_keys_model_group ON key_portal_api_keys(model_group)")
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_portal_key_pool (
                            api_key text PRIMARY KEY,
                            state text NOT NULL DEFAULT 'unused',
                            assigned_email text,
                            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            assigned_at timestamptz,
                            updated_at timestamptz NOT NULL DEFAULT now()
                        )
                        """
                    )
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_key_portal_key_pool_state ON key_portal_key_pool(state)")
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_portal_sessions (
                            session_id_hash text PRIMARY KEY,
                            email text NOT NULL,
                            user_data jsonb NOT NULL DEFAULT '{}'::jsonb,
                            request_meta jsonb NOT NULL DEFAULT '{}'::jsonb,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            updated_at timestamptz NOT NULL DEFAULT now(),
                            expires_at timestamptz NOT NULL,
                            revoked_at timestamptz
                        )
                        """
                    )
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_key_portal_sessions_email ON key_portal_sessions(email)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_key_portal_sessions_expires ON key_portal_sessions(expires_at)")
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_portal_approval_requests (
                            id bigserial PRIMARY KEY,
                            email text NOT NULL,
                            name text NOT NULL DEFAULT '',
                            label text NOT NULL DEFAULT '',
                            model_group text NOT NULL,
                            reason text NOT NULL DEFAULT '',
                            daily_budget text NOT NULL DEFAULT '',
                            instance_id text UNIQUE,
                            status text NOT NULL DEFAULT 'pending',
                            api_key text,
                            request_type text NOT NULL DEFAULT 'new_key',
                            request_payload jsonb NOT NULL DEFAULT '{}'::jsonb,
                            created_at timestamptz NOT NULL,
                            resolved_at timestamptz,
                            updated_at timestamptz NOT NULL DEFAULT now()
                        )
                        """
                    )
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_key_portal_approval_email ON key_portal_approval_requests(email)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_key_portal_approval_status ON key_portal_approval_requests(status)")
                    cur.execute(
                        """
                        SELECT setval(
                            pg_get_serial_sequence('key_portal_approval_requests', 'id'),
                            COALESCE((SELECT MAX(id) FROM key_portal_approval_requests), 0) + 1,
                            false
                        )
                        """
                    )
                    cur.execute(
                        """
                        CREATE TABLE IF NOT EXISTS key_portal_status_events (
                            id bigserial PRIMARY KEY,
                            event_type text NOT NULL,
                            dedupe_key text NOT NULL UNIQUE,
                            status text NOT NULL DEFAULT 'open',
                            severity text NOT NULL DEFAULT 'info',
                            title text NOT NULL,
                            summary text NOT NULL DEFAULT '',
                            reason text NOT NULL DEFAULT '',
                            affected_nodes jsonb NOT NULL DEFAULT '[]'::jsonb,
                            metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
                            admin_note text NOT NULL DEFAULT '',
                            started_at timestamptz NOT NULL DEFAULT now(),
                            resolved_at timestamptz,
                            last_seen_at timestamptz NOT NULL DEFAULT now(),
                            notified_at timestamptz,
                            created_at timestamptz NOT NULL DEFAULT now(),
                            updated_at timestamptz NOT NULL DEFAULT now()
                        )
                        """
                    )
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_key_portal_status_events_type_status ON key_portal_status_events(event_type, status)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_key_portal_status_events_started ON key_portal_status_events(started_at DESC)")
                    cur.execute("CREATE INDEX IF NOT EXISTS idx_key_portal_status_events_updated ON key_portal_status_events(updated_at DESC)")
                conn.commit()
            self._schema_ready = True
            print("[PortalState] Postgres schema ready")
            return True

    def load_user_keys(self):
        if not self.ensure_schema():
            return None
        with self.connect() as conn:
            users = {}
            keys = {}
            for row in conn.execute("SELECT * FROM key_portal_users ORDER BY email").fetchall():
                metadata = _json_value(row.get("metadata"))
                email = row.get("email", "")
                item = {
                    "email": email,
                    "name": row.get("name") or email,
                    "api_keys": [],
                    "created_at": _iso(row.get("created_at")),
                    **metadata,
                }
                for field in ("feishu_open_id", "feishu_user_id", "feishu_union_id", "avatar_url", "last_login_at"):
                    if row.get(field):
                        item[field] = _iso(row[field]) if field.endswith("_at") else row[field]
                users[email] = item

            rows = conn.execute(
                """
                SELECT * FROM key_portal_api_keys
                WHERE revoked_at IS NULL
                ORDER BY created_at ASC, api_key ASC
                """
            ).fetchall()
            for row in rows:
                metadata = _json_value(row.get("metadata"))
                api_key = row.get("api_key", "")
                email = row.get("email", "")
                item = {
                    "email": email,
                    "label": row.get("label") or "默认",
                    "model_group": row.get("model_group") or "common",
                    "source": row.get("source") or "cliproxy",
                    "status": row.get("status") or "active",
                    "created_at": _iso(row.get("created_at")),
                    "updated_at": _iso(row.get("updated_at")),
                    **metadata,
                }
                if row.get("max_budget") is not None:
                    item["max_budget"] = float(row["max_budget"])
                keys[api_key] = item
                users.setdefault(email, {"email": email, "name": email, "api_keys": [], "created_at": _iso(_now())})
                if api_key not in users[email].setdefault("api_keys", []):
                    users[email]["api_keys"].append(api_key)

        return {"version": "2.0", "users": users, "keys": keys}

    def save_user_keys(self, data):
        if not self.ensure_schema():
            return False
        users = (data or {}).get("users", {}) or {}
        keys = (data or {}).get("keys", {}) or {}
        core_user = {"email", "name", "api_keys", "created_at", "updated_at", "feishu_open_id", "feishu_user_id", "feishu_union_id", "avatar_url", "last_login_at"}
        core_key = {"email", "label", "model_group", "source", "status", "max_budget", "created_at", "updated_at"}
        now = _now()
        with self.connect() as conn:
            with conn.cursor() as cur:
                for email, user in users.items():
                    email = str(user.get("email") or email or "").strip().lower()
                    if not email:
                        continue
                    metadata = {k: v for k, v in user.items() if k not in core_user}
                    cur.execute(
                        """
                        INSERT INTO key_portal_users (
                            email, name, feishu_open_id, feishu_user_id, feishu_union_id,
                            avatar_url, metadata, created_at, updated_at, last_login_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), now(), %s)
                        ON CONFLICT (email) DO UPDATE SET
                            name = COALESCE(NULLIF(EXCLUDED.name, ''), key_portal_users.name),
                            feishu_open_id = COALESCE(EXCLUDED.feishu_open_id, key_portal_users.feishu_open_id),
                            feishu_user_id = COALESCE(EXCLUDED.feishu_user_id, key_portal_users.feishu_user_id),
                            feishu_union_id = COALESCE(EXCLUDED.feishu_union_id, key_portal_users.feishu_union_id),
                            avatar_url = COALESCE(EXCLUDED.avatar_url, key_portal_users.avatar_url),
                            metadata = key_portal_users.metadata || EXCLUDED.metadata,
                            updated_at = now(),
                            last_login_at = COALESCE(EXCLUDED.last_login_at, key_portal_users.last_login_at)
                        """,
                        (
                            email,
                            user.get("name") or email,
                            user.get("feishu_open_id"),
                            user.get("feishu_user_id"),
                            user.get("feishu_union_id"),
                            user.get("avatar_url"),
                            Jsonb(metadata),
                            user.get("created_at"),
                            user.get("last_login_at"),
                        ),
                    )

                active_keys = []
                for api_key, key_info in keys.items():
                    api_key = str(api_key or "").strip()
                    email = str(key_info.get("email", "")).strip().lower()
                    if not api_key or not email:
                        continue
                    if email not in users:
                        cur.execute(
                            """
                            INSERT INTO key_portal_users (email, name, created_at, updated_at)
                            VALUES (%s, %s, now(), now())
                            ON CONFLICT (email) DO NOTHING
                            """,
                            (email, email),
                        )
                    active_keys.append(api_key)
                    metadata = {k: v for k, v in key_info.items() if k not in core_key}
                    max_budget = key_info.get("max_budget")
                    cur.execute(
                        """
                        INSERT INTO key_portal_api_keys (
                            api_key, email, label, model_group, source, status,
                            max_budget, metadata, created_at, updated_at, revoked_at
                        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, COALESCE(%s, now()), now(), NULL)
                        ON CONFLICT (api_key) DO UPDATE SET
                            email = EXCLUDED.email,
                            label = EXCLUDED.label,
                            model_group = EXCLUDED.model_group,
                            source = EXCLUDED.source,
                            status = EXCLUDED.status,
                            max_budget = EXCLUDED.max_budget,
                            metadata = key_portal_api_keys.metadata || EXCLUDED.metadata,
                            updated_at = now(),
                            revoked_at = NULL
                        """,
                        (
                            api_key,
                            email,
                            key_info.get("label") or "默认",
                            key_info.get("model_group") or "common",
                            key_info.get("source") or "cliproxy",
                            key_info.get("status") or "active",
                            float(max_budget) if max_budget not in (None, "") else None,
                            Jsonb(metadata),
                            key_info.get("created_at"),
                        ),
                    )
            conn.commit()
        return True

    def load_key_pool(self):
        if not self.ensure_schema():
            return None
        with self.connect() as conn:
            rows = conn.execute("SELECT * FROM key_portal_key_pool ORDER BY created_at ASC, api_key ASC").fetchall()
        pool = {"unused": [], "assigned": {}}
        for row in rows:
            key = row.get("api_key")
            if not key:
                continue
            if row.get("state") == "assigned":
                pool["assigned"][key] = row.get("assigned_email") or ""
            elif row.get("state") == "unused":
                pool["unused"].append(key)
        pool["total"] = len(pool["unused"]) + len(pool["assigned"])
        return pool

    def save_key_pool(self, data):
        if not self.ensure_schema():
            return False
        unused = [str(k).strip() for k in (data or {}).get("unused", []) if str(k or "").strip()]
        assigned = {
            str(k).strip(): str(v or "").strip().lower()
            for k, v in ((data or {}).get("assigned", {}) or {}).items()
            if str(k or "").strip()
        }
        all_keys = sorted(set(unused) | set(assigned))
        with self.connect() as conn:
            with conn.cursor() as cur:
                for api_key in unused:
                    cur.execute(
                        """
                        INSERT INTO key_portal_key_pool (api_key, state, assigned_email, updated_at)
                        VALUES (%s, 'unused', NULL, now())
                        ON CONFLICT (api_key) DO UPDATE SET
                            state = 'unused', assigned_email = NULL, updated_at = now()
                        """,
                        (api_key,),
                    )
                for api_key, email in assigned.items():
                    cur.execute(
                        """
                        INSERT INTO key_portal_key_pool (api_key, state, assigned_email, assigned_at, updated_at)
                        VALUES (%s, 'assigned', %s, now(), now())
                        ON CONFLICT (api_key) DO UPDATE SET
                            state = 'assigned', assigned_email = EXCLUDED.assigned_email,
                            assigned_at = COALESCE(key_portal_key_pool.assigned_at, now()),
                            updated_at = now()
                        """,
                        (api_key, email),
                    )
                if all_keys:
                    cur.execute("DELETE FROM key_portal_key_pool WHERE NOT (api_key = ANY(%s))", (all_keys,))
            conn.commit()
        return True

    def create_session(self, email, user_data, ttl_seconds, request_meta):
        session_id = secrets.token_urlsafe(32)
        expires_at = _now() + timedelta(seconds=int(ttl_seconds or 0))
        payload = {
            "email": email,
            "user": user_data or {"email": email},
            "expires_at": _iso(expires_at),
            "created_at": _iso(_now()),
        }
        if self.redis_enabled:
            try:
                self._redis.setex(f"kp:session:{session_id}", int(ttl_seconds), json.dumps(payload, ensure_ascii=False))
            except Exception as exc:
                print(f"[PortalState] Redis session write failed: {exc}")
        with _session_lock:
            _session_memory[session_id] = payload
        if self.ensure_schema():
            with self.connect() as conn:
                conn.execute(
                    """
                    INSERT INTO key_portal_sessions (
                        session_id_hash, email, user_data, request_meta, expires_at
                    ) VALUES (%s, %s, %s, %s, %s)
                    ON CONFLICT (session_id_hash) DO UPDATE SET
                        email = EXCLUDED.email,
                        user_data = EXCLUDED.user_data,
                        request_meta = EXCLUDED.request_meta,
                        expires_at = EXCLUDED.expires_at,
                        revoked_at = NULL,
                        updated_at = now()
                    """,
                    (_session_hash(session_id), email, Jsonb(user_data or {}), Jsonb(request_meta or {}), expires_at),
                )
                conn.commit()
        return session_id

    def get_session(self, session_id):
        session_id = str(session_id or "").strip()
        if not session_id:
            return None
        if self.redis_enabled:
            try:
                raw = self._redis.get(f"kp:session:{session_id}")
                if raw:
                    return json.loads(raw)
            except Exception as exc:
                print(f"[PortalState] Redis session read failed: {exc}")
        with _session_lock:
            payload = _session_memory.get(session_id)
        if payload:
            try:
                expires_text = payload.get("expires_at", "")
                expires = datetime.fromisoformat(expires_text.replace("Z", "+00:00"))
                if expires > _now():
                    return payload
            except Exception:
                pass
        if self.ensure_schema():
            with self.connect() as conn:
                row = conn.execute(
                    "SELECT * FROM key_portal_sessions WHERE session_id_hash = %s",
                    (_session_hash(session_id),),
                ).fetchone()
            return _public_session_payload(row)
        return None

    def destroy_session(self, session_id):
        session_id = str(session_id or "").strip()
        if not session_id:
            return False
        if self.redis_enabled:
            try:
                self._redis.delete(f"kp:session:{session_id}")
            except Exception:
                pass
        with _session_lock:
            _session_memory.pop(session_id, None)
        if self.ensure_schema():
            with self.connect() as conn:
                conn.execute(
                    "UPDATE key_portal_sessions SET revoked_at = now(), updated_at = now() WHERE session_id_hash = %s",
                    (_session_hash(session_id),),
                )
                conn.commit()
        return True

    def create_oauth_state(self, payload, ttl_seconds):
        state = secrets.token_urlsafe(24)
        record = {
            "payload": payload or {},
            "expires_at": time.time() + int(ttl_seconds or 0),
        }
        if self.redis_enabled:
            try:
                self._redis.setex(f"kp:oauth-state:{state}", int(ttl_seconds), json.dumps(record, ensure_ascii=False))
                return state
            except Exception as exc:
                print(f"[PortalState] Redis oauth state write failed: {exc}")
        with _oauth_state_lock:
            _oauth_state_memory[state] = record
        return state

    def consume_oauth_state(self, state):
        state = str(state or "").strip()
        if not state:
            return None
        if self.redis_enabled:
            try:
                key = f"kp:oauth-state:{state}"
                raw = self._redis.get(key)
                self._redis.delete(key)
                if raw:
                    record = json.loads(raw)
                    if record.get("expires_at", 0) >= time.time():
                        return record.get("payload") or {}
            except Exception as exc:
                print(f"[PortalState] Redis oauth state read failed: {exc}")
        with _oauth_state_lock:
            record = _oauth_state_memory.pop(state, None)
        if not record or record.get("expires_at", 0) < time.time():
            return None
        return record.get("payload") or {}

    def upsert_status_event(
        self,
        event_type,
        dedupe_key,
        title,
        summary="",
        reason="",
        affected_nodes=None,
        metadata=None,
        status="open",
        severity="info",
        started_at=None,
    ):
        event_type = str(event_type or "").strip()
        dedupe_key = str(dedupe_key or "").strip()
        title = str(title or "").strip()
        status = str(status or "open").strip()
        severity = str(severity or "info").strip()
        now = _now()
        started_at = started_at or now
        affected_nodes = affected_nodes or []
        metadata = metadata or {}
        if not event_type or not dedupe_key or not title:
            return {"event": None, "created": False, "changed": False}

        if self.ensure_schema():
            with self.connect() as conn:
                existing = conn.execute(
                    "SELECT * FROM key_portal_status_events WHERE dedupe_key = %s",
                    (dedupe_key,),
                ).fetchone()
                created = existing is None
                reopening = bool(existing and status == "open" and existing.get("status") == "resolved")
                changed = created or reopening or existing.get("status") != status
                with conn.cursor() as cur:
                    if existing:
                        cur.execute(
                            """
                            UPDATE key_portal_status_events SET
                                event_type = %s,
                                status = %s,
                                severity = %s,
                                title = %s,
                                summary = %s,
                                reason = %s,
                                affected_nodes = %s,
                                metadata = %s,
                                started_at = CASE WHEN %s THEN %s ELSE started_at END,
                                resolved_at = CASE WHEN %s = 'open' THEN NULL ELSE resolved_at END,
                                notified_at = CASE WHEN %s THEN NULL ELSE notified_at END,
                                last_seen_at = %s,
                                updated_at = now()
                            WHERE dedupe_key = %s
                            RETURNING *
                            """,
                            (
                                event_type,
                                status,
                                severity,
                                title,
                                summary or "",
                                reason or "",
                                Jsonb(affected_nodes),
                                Jsonb(metadata),
                                reopening,
                                started_at,
                                status,
                                reopening,
                                now,
                                dedupe_key,
                            ),
                        )
                    else:
                        cur.execute(
                            """
                            INSERT INTO key_portal_status_events (
                                event_type, dedupe_key, status, severity, title, summary,
                                reason, affected_nodes, metadata, started_at, last_seen_at
                            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                            RETURNING *
                            """,
                            (
                                event_type,
                                dedupe_key,
                                status,
                                severity,
                                title,
                                summary or "",
                                reason or "",
                                Jsonb(affected_nodes),
                                Jsonb(metadata),
                                started_at,
                                now,
                            ),
                        )
                    row = cur.fetchone()
                conn.commit()
            return {"event": _status_event_payload(row), "created": created, "changed": changed}

        global _status_event_next_id
        with _status_events_lock:
            existing = _status_events_memory.get(dedupe_key)
            created = existing is None
            reopening = bool(existing and status == "open" and existing.get("status") == "resolved")
            changed = created or reopening or existing.get("status") != status
            if existing:
                existing.update({
                    "event_type": event_type,
                    "status": status,
                    "severity": severity,
                    "title": title,
                    "summary": summary or "",
                    "reason": reason or "",
                    "affected_nodes": affected_nodes,
                    "metadata": metadata,
                    "started_at": started_at if reopening else existing.get("started_at"),
                    "resolved_at": None if status == "open" else existing.get("resolved_at"),
                    "notified_at": None if reopening else existing.get("notified_at"),
                    "last_seen_at": now,
                    "updated_at": now,
                })
                row = existing
            else:
                row = {
                    "id": _status_event_next_id,
                    "event_type": event_type,
                    "dedupe_key": dedupe_key,
                    "status": status,
                    "severity": severity,
                    "title": title,
                    "summary": summary or "",
                    "reason": reason or "",
                    "affected_nodes": affected_nodes,
                    "metadata": metadata,
                    "admin_note": "",
                    "started_at": started_at,
                    "resolved_at": None,
                    "last_seen_at": now,
                    "notified_at": None,
                    "created_at": now,
                    "updated_at": now,
                }
                _status_event_next_id += 1
                _status_events_memory[dedupe_key] = row
        return {"event": _memory_status_event_payload(row), "created": created, "changed": changed}

    def resolve_status_event(self, dedupe_key, summary="", reason="", metadata=None, resolved_at=None):
        dedupe_key = str(dedupe_key or "").strip()
        if not dedupe_key:
            return {"event": None, "changed": False}
        metadata = metadata or {}
        resolved_at = resolved_at or _now()

        if self.ensure_schema():
            with self.connect() as conn:
                existing = conn.execute(
                    "SELECT * FROM key_portal_status_events WHERE dedupe_key = %s",
                    (dedupe_key,),
                ).fetchone()
                if not existing:
                    return {"event": None, "changed": False}
                changed = existing.get("status") == "open"
                if not changed:
                    return {"event": _status_event_payload(existing), "changed": False}
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE key_portal_status_events SET
                            status = 'resolved',
                            summary = COALESCE(NULLIF(%s, ''), summary),
                            reason = COALESCE(NULLIF(%s, ''), reason),
                            metadata = metadata || %s,
                            resolved_at = COALESCE(resolved_at, %s),
                            last_seen_at = %s,
                            updated_at = now()
                        WHERE dedupe_key = %s
                        RETURNING *
                        """,
                        (summary or "", reason or "", Jsonb(metadata), resolved_at, resolved_at, dedupe_key),
                    )
                    row = cur.fetchone()
                conn.commit()
            return {"event": _status_event_payload(row), "changed": True}

        with _status_events_lock:
            row = _status_events_memory.get(dedupe_key)
            if not row:
                return {"event": None, "changed": False}
            changed = row.get("status") == "open"
            if changed:
                row["status"] = "resolved"
                if summary:
                    row["summary"] = summary
                if reason:
                    row["reason"] = reason
                row["metadata"] = {**(row.get("metadata") or {}), **metadata}
                row["resolved_at"] = row.get("resolved_at") or resolved_at
                row["last_seen_at"] = resolved_at
                row["updated_at"] = resolved_at
        return {"event": _memory_status_event_payload(row), "changed": changed}

    def mark_status_event_notified(self, event_id):
        if not event_id:
            return False
        now = _now()
        if self.ensure_schema():
            with self.connect() as conn:
                conn.execute(
                    "UPDATE key_portal_status_events SET notified_at = %s, updated_at = now() WHERE id = %s",
                    (now, event_id),
                )
                conn.commit()
            return True
        with _status_events_lock:
            for row in _status_events_memory.values():
                if row.get("id") == event_id:
                    row["notified_at"] = now
                    row["updated_at"] = now
                    return True
        return False

    def list_status_events(self, limit=100):
        try:
            limit = min(max(int(limit or 100), 1), 500)
        except ValueError:
            limit = 100
        if self.ensure_schema():
            with self.connect() as conn:
                rows = conn.execute(
                    """
                    SELECT * FROM key_portal_status_events
                    ORDER BY
                        CASE WHEN status = 'open' THEN 0 ELSE 1 END,
                        started_at DESC,
                        id DESC
                    LIMIT %s
                    """,
                    (limit,),
                ).fetchall()
            return [_status_event_payload(row) for row in rows]
        with _status_events_lock:
            rows = sorted(
                _status_events_memory.values(),
                key=lambda item: (0 if item.get("status") == "open" else 1, item.get("started_at") or _now(), item.get("id") or 0),
                reverse=False,
            )
        return [_memory_status_event_payload(row) for row in rows[:limit]]

    def update_status_event_note(self, event_id, admin_note):
        try:
            event_id = int(event_id)
        except Exception:
            return None
        admin_note = str(admin_note or "").strip()
        if self.ensure_schema():
            with self.connect() as conn:
                with conn.cursor() as cur:
                    cur.execute(
                        """
                        UPDATE key_portal_status_events
                        SET admin_note = %s, updated_at = now()
                        WHERE id = %s
                        RETURNING *
                        """,
                        (admin_note, event_id),
                    )
                    row = cur.fetchone()
                conn.commit()
            return _status_event_payload(row)
        with _status_events_lock:
            for row in _status_events_memory.values():
                if row.get("id") == event_id:
                    row["admin_note"] = admin_note
                    row["updated_at"] = _now()
                    return _memory_status_event_payload(row)
        return None

    def cache_get_json(self, key):
        if not self.redis_enabled:
            return None
        try:
            raw = self._redis.get(f"kp:cache:{key}")
            return json.loads(raw) if raw else None
        except Exception as exc:
            print(f"[PortalState] Redis cache read failed for {key}: {exc}")
            return None

    def cache_set_json(self, key, value, ttl_seconds=60):
        if not self.redis_enabled:
            return False
        try:
            self._redis.setex(f"kp:cache:{key}", int(ttl_seconds), json.dumps(value, ensure_ascii=False))
            return True
        except Exception as exc:
            print(f"[PortalState] Redis cache write failed for {key}: {exc}")
            return False

    def cache_delete(self, key):
        if not self.redis_enabled:
            return False
        try:
            self._redis.delete(f"kp:cache:{key}")
            return True
        except Exception:
            return False
