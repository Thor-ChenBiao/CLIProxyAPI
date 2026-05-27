"""
Feishu Approval integration for expensive model groups.

Flow:
1. User requests a key for an approval-required model group (claude, deepseek)
2. Key Portal creates a Feishu approval instance via Open API
3. Approval is routed to the user's leader + a fixed approver (configured in Feishu)
4. On approval/rejection, Feishu sends a webhook callback to /api/feishu/approval-callback
5. If approved, Key Portal automatically issues the key and notifies the user
"""

import hashlib
import hmac
import json
import os
import sqlite3
import time
from datetime import datetime, timezone, timedelta

import requests

import config
import feishu
import portal_state

DB_FILE = os.path.join(os.path.dirname(__file__), "data", "usage.db")

APPROVAL_CODE = config.FEISHU_APPROVAL_CODE

APPROVAL_REQUIRED_GROUPS = {"claude", "deepseek"}
REQUEST_TYPE_NEW_KEY = "new_key"
REQUEST_TYPE_QUOTA_TOPUP = "quota_topup"

# Independent token cache for the approval-specific Feishu app
_approval_token_cache = {"token": None, "expires_at": 0}


def _get_approval_token():
    """Get tenant_access_token for the approval-dedicated Feishu app."""
    if not config.FEISHU_APPROVAL_APP_ID or not config.FEISHU_APPROVAL_APP_SECRET:
        # Fallback to the shared notification app
        return feishu.get_feishu_access_token()

    if _approval_token_cache["token"] and _approval_token_cache["expires_at"] > time.time():
        return _approval_token_cache["token"]

    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal",
            json={
                "app_id": config.FEISHU_APPROVAL_APP_ID,
                "app_secret": config.FEISHU_APPROVAL_APP_SECRET,
            },
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            token = data["tenant_access_token"]
            expire = data.get("expire", 7200)
            _approval_token_cache["token"] = token
            _approval_token_cache["expires_at"] = time.time() + expire - 60
            return token
        else:
            print(f"[Approval] Token error: {data}")
            return None
    except Exception as e:
        print(f"[Approval] Token request failed: {e}")
        return None

BEIJING_TZ = timezone(timedelta(hours=8))


def _pg_enabled():
    return portal_state.is_pg_enabled() and portal_state.ensure_schema()


def _pg_row_to_dict(row):
    if not row:
        return None
    data = dict(row)
    payload = data.get("request_payload")
    if isinstance(payload, (dict, list)):
        data["request_payload"] = json.dumps(payload, ensure_ascii=False)
    return data


def _pg_conn():
    return portal_state.current().connect()


def _ensure_column(cursor, table, column, definition):
    columns = [row[1] for row in cursor.execute(f"PRAGMA table_info({table})").fetchall()]
    if column not in columns:
        cursor.execute(f"ALTER TABLE {table} ADD COLUMN {column} {definition}")


def init_approval_table():
    if _pg_enabled():
        print("[Approval] Using Postgres approval_requests table")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        CREATE TABLE IF NOT EXISTS approval_requests (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            email TEXT NOT NULL,
            name TEXT NOT NULL DEFAULT '',
            label TEXT NOT NULL DEFAULT '',
            model_group TEXT NOT NULL,
            reason TEXT NOT NULL DEFAULT '',
            daily_budget TEXT NOT NULL DEFAULT '',
            instance_id TEXT UNIQUE,
            status TEXT NOT NULL DEFAULT 'pending',
            api_key TEXT DEFAULT NULL,
            created_at TEXT NOT NULL,
            resolved_at TEXT DEFAULT NULL,
            request_type TEXT NOT NULL DEFAULT 'new_key',
            request_payload TEXT NOT NULL DEFAULT ''
        )
    """)
    _ensure_column(cursor, "approval_requests", "request_type", "TEXT NOT NULL DEFAULT 'new_key'")
    _ensure_column(cursor, "approval_requests", "request_payload", "TEXT NOT NULL DEFAULT ''")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_email ON approval_requests(email)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_status ON approval_requests(status)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_instance ON approval_requests(instance_id)")
    cursor.execute("CREATE INDEX IF NOT EXISTS idx_approval_request_type ON approval_requests(request_type)")
    conn.commit()
    conn.close()


def requires_approval(model_group):
    return model_group in APPROVAL_REQUIRED_GROUPS


def _insert_approval_request(email, name, label, model_group, reason, daily_budget, request_type, request_payload):
    now = datetime.now(BEIJING_TZ).isoformat()
    if _pg_enabled():
        with _pg_conn() as conn:
            row = conn.execute(
                """
                INSERT INTO key_portal_approval_requests (
                    email, name, label, model_group, reason, daily_budget,
                    status, created_at, request_type, request_payload
                ) VALUES (%s, %s, %s, %s, %s, %s, 'pending', %s, %s, %s)
                RETURNING id
                """,
                (
                    email, name, label, model_group, reason, daily_budget,
                    now, request_type, portal_state.Jsonb(request_payload or {}),
                ),
            ).fetchone()
            conn.commit()
            return int(row["id"])

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("""
        INSERT INTO approval_requests (
            email, name, label, model_group, reason, daily_budget, status, created_at, request_type, request_payload
        ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?, ?, ?)
    """, (email, name, label, model_group, reason, daily_budget, now, request_type, json.dumps(request_payload or {}, ensure_ascii=False)))
    request_id = cursor.lastrowid
    conn.commit()
    conn.close()
    return request_id


def _attach_instance_id(request_id, instance_id):
    if _pg_enabled():
        with _pg_conn() as conn:
            conn.execute(
                "UPDATE key_portal_approval_requests SET instance_id = %s, updated_at = now() WHERE id = %s",
                (instance_id, request_id),
            )
            conn.commit()
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute("UPDATE approval_requests SET instance_id = ? WHERE id = ?", (instance_id, request_id))
    conn.commit()
    conn.close()


def create_approval_request(email, name, label, model_group, reason="", daily_budget=""):
    """Create a local approval record and submit to Feishu."""
    request_id = _insert_approval_request(
        email, name, label, model_group, reason, daily_budget, REQUEST_TYPE_NEW_KEY, {}
    )

    instance_id, error = _submit_feishu_approval(request_id, email, name, model_group, reason, daily_budget)
    if error:
        _update_request_status(request_id, "failed", error_msg=error)
        return None, error

    _attach_instance_id(request_id, instance_id)
    return instance_id, None


def create_quota_topup_request(email, name, label, model_group, api_key, current_budget, additional_budget, reason=""):
    daily_budget = f"+${additional_budget:g}"
    payload = {
        "api_key": api_key,
        "current_budget": current_budget,
        "additional_budget": additional_budget,
        "new_budget": round(float(current_budget or 0) + float(additional_budget), 6),
    }
    request_id = _insert_approval_request(
        email, name, label, model_group, reason, daily_budget, REQUEST_TYPE_QUOTA_TOPUP, payload
    )

    instance_id, error = _submit_feishu_approval(request_id, email, name, model_group, reason, daily_budget, request_type=REQUEST_TYPE_QUOTA_TOPUP)
    if error:
        _update_request_status(request_id, "failed", error_msg=error)
        return None, error

    _attach_instance_id(request_id, instance_id)
    return instance_id, None


def _submit_feishu_approval(request_id, email, name, model_group, reason, daily_budget, request_type=REQUEST_TYPE_NEW_KEY):
    """Submit approval instance to Feishu Open API."""
    token = _get_approval_token()
    if not token:
        return None, "无法获取飞书 access token"

    model_group_labels = {
        "claude": "Claude (Bedrock)",
        "deepseek": "DeepSeek",
        "gemini": "Gemini",
    }

    request_labels = {
        REQUEST_TYPE_NEW_KEY: "新 Key 申请",
        REQUEST_TYPE_QUOTA_TOPUP: "现有 Key 追加额度",
    }
    form_reason = reason or "日常开发使用"
    if request_type == REQUEST_TYPE_QUOTA_TOPUP:
        form_reason = f"【{request_labels[request_type]}】{form_reason}"

    form_data = [
        {"id": "widget16496668458931544023579901857", "type": "input", "value": model_group_labels.get(model_group, model_group)},
        {"id": "widget17794338370490001", "type": "input", "value": daily_budget or "未指定"},
        {"id": "widget16496668457671184511036511776", "type": "textarea", "value": form_reason},
    ]

    payload = {
        "approval_code": APPROVAL_CODE,
        "form": json.dumps(form_data),
    }

    applicant_open_id = _lookup_feishu_user_id(email, token)
    if not applicant_open_id:
        return None, f"无法在飞书通讯录中找到申请人：{email}"
    payload["user_id"] = applicant_open_id

    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/approval/v4/instances",
            params={"user_id_type": "open_id"},
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=15,
        )
        data = resp.json()
        if data.get("code") == 0:
            instance_id = data.get("data", {}).get("instance_code", "")
            print(f"[Approval] Created instance {instance_id} for {email} ({model_group})")
            return instance_id, None
        else:
            err_msg = data.get("msg", str(data))
            print(f"[Approval] Feishu API error: {err_msg}")
            return None, f"飞书审批创建失败: {err_msg}"
    except Exception as e:
        print(f"[Approval] Request failed: {e}")
        return None, f"飞书审批请求异常: {e}"


def _lookup_feishu_user_id(email, token):
    """Look up user's open_id by email."""
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/contact/v3/users/batch_get_id",
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
            },
            params={"user_id_type": "open_id"},
            json={"emails": [email]},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") != 0:
            print(f"[Approval] User lookup failed for {email}: {data}")
            return None
        user_list = data.get("data", {}).get("user_list", [])
        if user_list and user_list[0].get("user_id"):
            return user_list[0]["user_id"]
        print(f"[Approval] User lookup returned no user for {email}: {data}")
    except Exception as e:
        print(f"[Approval] User lookup failed for {email}: {e}")
    return None


def handle_approval_callback(payload):
    """Process Feishu approval event callback.

    Returns (success: bool, message: str)
    """
    event = payload.get("event", {})
    instance_id = event.get("instance_code") or event.get("instance_id", "")
    status = event.get("status", "").upper()

    if not instance_id:
        return False, "missing instance_id"

    if _pg_enabled():
        with _pg_conn() as conn:
            row = conn.execute(
                "SELECT * FROM key_portal_approval_requests WHERE instance_id = %s",
                (instance_id,),
            ).fetchone()
        row = _pg_row_to_dict(row)
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        row = cursor.execute(
            "SELECT * FROM approval_requests WHERE instance_id = ?", (instance_id,)
        ).fetchone()
        conn.close()
        row = dict(row) if row else None

    if not row:
        print(f"[Approval] Callback for unknown instance {instance_id}")
        return False, "unknown instance"

    if row["status"] != "pending":
        return True, "already processed"

    if status == "APPROVED":
        return _on_approved(dict(row))
    elif status in ("REJECTED", "CANCELED", "DELETED"):
        _update_request_status(row["id"], "rejected")
        _notify_user_rejected(row["email"], row["model_group"], row.get("request_type") or REQUEST_TYPE_NEW_KEY)
        print(f"[Approval] Rejected: {row['email']} ({row['model_group']})")
        return True, "rejected"
    else:
        print(f"[Approval] Unknown status '{status}' for {instance_id}")
        return True, "ignored"


def _parse_budget(daily_budget_str):
    """Parse budget string like '$5', '5.0', '10' into a float."""
    if not daily_budget_str:
        return None
    s = daily_budget_str.strip().replace("$", "").replace("￥", "").replace("元", "").replace("美元", "")
    try:
        val = float(s)
        return val if val > 0 else None
    except (ValueError, TypeError):
        return None


def _request_payload(row):
    try:
        return json.loads(row.get("request_payload") or "{}")
    except Exception:
        return {}


def _on_approved(row):
    request_type = row.get("request_type") or REQUEST_TYPE_NEW_KEY
    if request_type == REQUEST_TYPE_QUOTA_TOPUP:
        return _on_quota_topup_approved(row)
    return _on_new_key_approved(row)


def _mark_request_approved(request_id, api_key):
    now = datetime.now(BEIJING_TZ).isoformat()
    if _pg_enabled():
        with _pg_conn() as conn:
            result = conn.execute(
                """
                UPDATE key_portal_approval_requests
                SET status = 'approved', api_key = %s, resolved_at = %s, updated_at = now()
                WHERE id = %s AND status = 'pending'
                """,
                (api_key, now, request_id),
            )
            changed = result.rowcount
            conn.commit()
        return changed

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE approval_requests SET status = 'approved', api_key = ?, resolved_at = ? WHERE id = ? AND status = 'pending'",
        (api_key, now, request_id)
    )
    changed = cursor.rowcount
    conn.commit()
    conn.close()
    return changed


def _on_new_key_approved(row):
    """Issue key after approval passes."""
    from app import assign_key_to_user

    email = row["email"]
    name = row["name"]
    label = row["label"]
    model_group = row["model_group"]
    max_budget = _parse_budget(row.get("daily_budget"))

    api_key, error = assign_key_to_user(email, name, label, model_group, max_budget=max_budget)
    if error:
        if "500" in str(error) or "timeout" in str(error).lower() or "connection" in str(error).lower():
            print(f"[Approval] Transient error for {email}, will retry: {error}")
            return False, f"transient error, will retry: {error}"
        _update_request_status(row["id"], "failed", error_msg=error)
        print(f"[Approval] Key issuance failed for {email}: {error}")
        return False, f"key issuance failed: {error}"

    changed = _mark_request_approved(row["id"], api_key)
    if not changed:
        return True, "already processed"

    _notify_user_approved(email, model_group, api_key, max_budget=max_budget)
    print(f"[Approval] Approved and key issued for {email} ({model_group})")
    return True, "approved"


def _on_quota_topup_approved(row):
    from app import update_litellm_key_budget, update_user_key_budget, mask_api_key

    email = row["email"]
    model_group = row["model_group"]
    payload = _request_payload(row)
    api_key = str(payload.get("api_key") or "")
    current_budget = _parse_budget(str(payload.get("current_budget") or "")) or 0
    additional_budget = _parse_budget(str(payload.get("additional_budget") or ""))
    if not api_key or not additional_budget:
        _update_request_status(row["id"], "failed", error_msg="invalid quota top-up payload")
        return False, "invalid quota top-up payload"

    new_budget = round(current_budget + additional_budget, 6)
    success, error = update_litellm_key_budget(api_key, new_budget)
    if not success:
        if "500" in str(error) or "timeout" in str(error).lower() or "connection" in str(error).lower():
            print(f"[Approval] Transient top-up error for {email}, will retry: {error}")
            return False, f"transient error, will retry: {error}"
        _update_request_status(row["id"], "failed", error_msg=error)
        print(f"[Approval] Top-up failed for {email}: {error}")
        return False, f"top-up failed: {error}"

    update_user_key_budget(api_key, new_budget)
    changed = _mark_request_approved(row["id"], api_key)
    if not changed:
        return True, "already processed"

    _notify_user_topup_approved(email, model_group, api_key, current_budget, additional_budget, new_budget)
    print(f"[Approval] Approved top-up for {email} {mask_api_key(api_key)} {current_budget} + {additional_budget} -> {new_budget}")
    return True, "approved"


def _update_request_status(request_id, status, error_msg=None):
    now = datetime.now(BEIJING_TZ).isoformat()
    if _pg_enabled():
        with _pg_conn() as conn:
            conn.execute(
                """
                UPDATE key_portal_approval_requests
                SET status = %s, resolved_at = %s, updated_at = now()
                WHERE id = %s
                """,
                (status, now, request_id),
            )
            conn.commit()
        if error_msg:
            print(f"[Approval] Request {request_id} -> {status}: {error_msg}")
        return

    conn = sqlite3.connect(DB_FILE)
    cursor = conn.cursor()
    cursor.execute(
        "UPDATE approval_requests SET status = ?, resolved_at = ? WHERE id = ?",
        (status, now, request_id)
    )
    conn.commit()
    conn.close()
    if error_msg:
        print(f"[Approval] Request {request_id} -> {status}: {error_msg}")


def _budget_estimate(model_group, budget):
    """Estimate tokens and requests from budget."""
    pricing = {
        "claude": {"input": 5, "output": 25, "label": "Claude Opus 4.6"},
        "deepseek": {"input": 0.14, "output": 0.28, "label": "DeepSeek"},
        "gemini": {"input": 1.25, "output": 10, "label": "Gemini Pro"},
    }
    p = pricing.get(model_group)
    if not p or not budget:
        return ""
    avg_cost_per_m = (p["input"] * 3 + p["output"] * 1) / 4
    m_tokens = budget / avg_cost_per_m
    requests_est = int(m_tokens * 1_000_000 / 4000)
    if m_tokens >= 1:
        token_str = f"{m_tokens:.1f}M"
    else:
        token_str = f"{int(m_tokens * 1000)}K"
    return f"${budget} ≈ {token_str} tokens ≈ {requests_est} 次请求（{p['label']}）"


def _notify_user_approved(email, model_group, api_key, max_budget=None):
    """Send rich Feishu card with key and setup instructions."""
    group_labels = {"claude": "Claude", "deepseek": "DeepSeek", "gemini": "Gemini"}
    group_name = group_labels.get(model_group, model_group)
    base_url = "https://token.zasdas.com"
    portal_url = "https://token.zasdas.com"

    setup_cmd = (
        f"node -e \"const fs=require('fs'),path=require('path'),os=require('os');"
        f"const dir=path.join(os.homedir(),'.claude');"
        f"const sf=path.join(dir,'settings.json');"
        f"if(!fs.existsSync(dir))fs.mkdirSync(dir,{{recursive:true}});"
        f"const s=fs.existsSync(sf)?JSON.parse(fs.readFileSync(sf,'utf8')):{{}};"
        f"s.apiKeyHelper='echo {api_key}';"
        f"if(!s.env)s.env={{}};"
        f"s.env.ANTHROPIC_BASE_URL='{base_url}';"
        f"fs.writeFileSync(sf,JSON.stringify(s,null,2));"
        f"console.log('Done!');\""
    )

    budget_info = ""
    if max_budget:
        estimate = _budget_estimate(model_group, max_budget)
        budget_info = f"\n\n**总额度：**${max_budget}（{estimate}）"

    model_tips = {
        "claude": "当前 Claude 仅支持 Opus 4.6，模型名请选 claude-opus-4-6（暂不支持 4.7）。",
        "deepseek": "可用模型：deepseek-chat（V3）、deepseek-reasoner（R1）。",
        "gemini": "可用模型：gemini-2.5-pro、gemini-2.5-flash。",
    }
    tip = model_tips.get(model_group, "")

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{group_name} Key 审批通过"},
            "template": "green"
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"你申请的 **{group_name}** 模型组 Key 已审批通过并自动生成。{budget_info}"}
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "fields": [
                    {"is_short": False, "text": {"tag": "lark_md", "content": f"**API Key**\n{api_key}"}},
                    {"is_short": False, "text": {"tag": "lark_md", "content": f"**API Base URL**\n{base_url}"}},
                ]
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": "**Claude Code 一键配置**\n复制以下命令到终端执行即可完成配置："}
            },
            {
                "tag": "div",
                "text": {"tag": "plain_text", "content": setup_cmd}
            },
            {"tag": "hr"},
            {
                "tag": "note",
                "elements": [{"tag": "plain_text", "content": f"{tip} 预算用完后当天请求将被拒绝，次日自动重置。如需更多额度请重新申请。多个 Key 切换推荐安装 CC Switch：https://github.com/farion1231/cc-switch"}]
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "查看我的 Keys"},
                        "type": "primary",
                        "url": f"{portal_url}/my-keys"
                    },
                    {
                        "tag": "button",
                        "text": {"tag": "plain_text", "content": "CC Switch (多Key切换)"},
                        "type": "default",
                        "url": "https://github.com/farion1231/cc-switch"
                    }
                ]
            }
        ]
    }

    _send_card(email, card)


def _send_card(email, card):
    token = feishu.get_feishu_access_token()
    if not token:
        print(f"[Approval] No token available for {email}")
        return
    try:
        resp = requests.post(
            "https://open.feishu.cn/open-apis/im/v1/messages",
            params={"receive_id_type": "email"},
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json={"receive_id": email, "msg_type": "interactive", "content": json.dumps(card, ensure_ascii=False)},
            timeout=10,
        )
        data = resp.json()
        if data.get("code") == 0:
            print(f"[Approval] Sent card to {email}")
        else:
            print(f"[Approval] Send failed for {email}: {data}")
    except Exception as e:
        print(f"[Approval] Send error for {email}: {e}")


def _notify_user_topup_approved(email, model_group, api_key, current_budget, additional_budget, new_budget):
    group_labels = {"claude": "Claude", "deepseek": "DeepSeek", "gemini": "Gemini"}
    group_name = group_labels.get(model_group, model_group)
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"{group_name} Key 额度追加已通过"},
            "template": "green"
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": f"你申请的 **{group_name}** Key 额度追加已审批通过并自动生效。"}
            },
            {"tag": "hr"},
            {
                "tag": "div",
                "fields": [
                    {"is_short": False, "text": {"tag": "lark_md", "content": f"**API Key**\n{api_key}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**原总额度**\n${current_budget:g}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**追加额度**\n+${additional_budget:g}"}},
                    {"is_short": True, "text": {"tag": "lark_md", "content": f"**新总额度**\n${new_budget:g}"}},
                ]
            },
            {"tag": "hr"},
            {
                "tag": "action",
                "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": "查看我的 Keys"},
                    "type": "primary",
                    "url": "https://token.zasdas.com/my-keys"
                }]
            }
        ]
    }
    _send_card(email, card)


def _notify_user_rejected(email, model_group, request_type=REQUEST_TYPE_NEW_KEY):
    group_labels = {"claude": "Claude", "deepseek": "DeepSeek", "gemini": "Gemini"}
    group_name = group_labels.get(model_group, model_group)
    title = f"{group_name} Key 审批被拒绝"
    body = f"你申请的 **{group_name}** 模型组 Key 审批未通过。\n\n如有疑问请联系管理员。"
    if request_type == REQUEST_TYPE_QUOTA_TOPUP:
        title = f"{group_name} Key 额度追加审批被拒绝"
        body = f"你申请的 **{group_name}** Key 额度追加审批未通过。\n\n如有疑问请联系管理员。"
    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": title},
            "template": "red"
        },
        "elements": [
            {
                "tag": "div",
                "text": {"tag": "lark_md", "content": body}
            }
        ]
    }
    _send_card(email, card)


def _portal_url():
    base = os.environ.get("PUBLIC_BASE_URL", "").strip()
    if base:
        return base.rstrip("/")
    return "http://localhost:8080"


def get_pending_approvals(email=None):
    if _pg_enabled():
        with _pg_conn() as conn:
            if email:
                rows = conn.execute(
                    "SELECT * FROM key_portal_approval_requests WHERE email = %s ORDER BY created_at DESC",
                    (email,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM key_portal_approval_requests ORDER BY created_at DESC"
                ).fetchall()
        return [_pg_row_to_dict(r) for r in rows]

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    if email:
        rows = cursor.execute(
            "SELECT * FROM approval_requests WHERE email = ? ORDER BY created_at DESC", (email,)
        ).fetchall()
    else:
        rows = cursor.execute(
            "SELECT * FROM approval_requests ORDER BY created_at DESC"
        ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def get_approval_by_instance(instance_id):
    if _pg_enabled():
        with _pg_conn() as conn:
            row = conn.execute(
                "SELECT * FROM key_portal_approval_requests WHERE instance_id = %s",
                (instance_id,),
            ).fetchone()
        return _pg_row_to_dict(row)

    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    cursor = conn.cursor()
    row = cursor.execute(
        "SELECT * FROM approval_requests WHERE instance_id = ?", (instance_id,)
    ).fetchone()
    conn.close()
    return dict(row) if row else None


def verify_callback_token(token):
    """Verify Feishu event callback verification token."""
    if not config.FEISHU_APPROVAL_VERIFICATION_TOKEN:
        return True
    return hmac.compare_digest(token, config.FEISHU_APPROVAL_VERIFICATION_TOKEN)


def poll_pending_approvals():
    """Check Feishu for status updates on pending approvals (fallback for missed callbacks)."""
    if _pg_enabled():
        with _pg_conn() as conn:
            pending = conn.execute(
                "SELECT * FROM key_portal_approval_requests WHERE status = 'pending' AND COALESCE(instance_id, '') != ''"
            ).fetchall()
        pending = [_pg_row_to_dict(row) for row in pending]
    else:
        conn = sqlite3.connect(DB_FILE)
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        pending = cursor.execute(
            "SELECT * FROM approval_requests WHERE status = 'pending' AND instance_id != ''"
        ).fetchall()
        conn.close()
        pending = [dict(row) for row in pending]

    if not pending:
        return

    token = _get_approval_token()
    if not token:
        return

    for row in pending:
        instance_id = row["instance_id"]
        try:
            resp = requests.get(
                f"https://open.feishu.cn/open-apis/approval/v4/instances/{instance_id}",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            data = resp.json()
            status = data.get("data", {}).get("status", "").upper()

            if status == "APPROVED":
                _on_approved(dict(row))
            elif status in ("REJECTED", "CANCELED", "DELETED"):
                _update_request_status(row["id"], "rejected")
                _notify_user_rejected(row["email"], row["model_group"], row["request_type"] if "request_type" in row.keys() else REQUEST_TYPE_NEW_KEY)
                print(f"[Approval] Poll: rejected {row['email']} ({row['model_group']})")
        except Exception as e:
            print(f"[Approval] Poll error for {instance_id}: {e}")
