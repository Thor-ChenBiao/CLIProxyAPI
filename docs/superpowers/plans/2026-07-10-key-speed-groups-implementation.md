# API Key Speed Groups Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let authenticated users choose Standard or Fast as the default execution mode for each owned API key while preserving explicit per-request `service_tier` values.

**Architecture:** Store `speed_group` in LiteLLM virtual-key metadata and apply it through a standalone LiteLLM `CustomLogger` pre-call hook. Key Portal owns validation, ownership checks, key creation, approval propagation, and UI; LiteLLM metadata remains authoritative and shared by node A and node B.

**Tech Stack:** Python 3.9 Flask Key Portal, LiteLLM 1.91.1/Python 3.12 callback API, Postgres JSONB metadata, vanilla JavaScript, unittest, systemd, AWS NLB.

## Global Constraints

- Missing or invalid speed metadata means `standard`.
- Explicit non-empty request `service_tier` always wins.
- Never select speed by model and never create Fast model aliases.
- Never perform capacity-based fallback.
- Do not modify Nginx, NLB routing, or CLIProxyAPI core behavior.
- Users can switch their own keys immediately without approval.
- Offline, batch, and background tasks must be directed to Standard mode.
- Do not overwrite or revert the existing uncommitted Key Portal production changes.
- Never log or print a full API key.

---

### Task 1: LiteLLM Speed Policy Hook

**Files:**
- Create: `ops/litellm/speed_group_callback.py`
- Create: `ops/litellm/test_speed_group_callback.py`

**Interfaces:**
- Consumes: `UserAPIKeyAuth.metadata` and normalized LiteLLM request `data`.
- Produces: `SpeedGroupCallback.async_pre_call_hook(...) -> dict` and module instance `speed_group_callback`.

- [ ] **Step 1: Write failing hook tests**

```python
class SpeedGroupCallbackTests(unittest.IsolatedAsyncioTestCase):
    async def apply(self, metadata, data):
        auth = SimpleNamespace(metadata=metadata)
        return await speed_group_callback.async_pre_call_hook(auth, None, dict(data), "responses")

    async def test_fast_key_defaults_to_priority(self):
        self.assertEqual((await self.apply({"speed_group": "fast"}, {}))["service_tier"], "priority")

    async def test_explicit_standard_wins_for_fast_key(self):
        result = await self.apply({"speed_group": "fast"}, {"service_tier": "default"})
        self.assertEqual(result["service_tier"], "default")

    async def test_explicit_fast_wins_for_standard_key(self):
        result = await self.apply({"speed_group": "standard"}, {"service_tier": "priority"})
        self.assertEqual(result["service_tier"], "priority")

    async def test_missing_or_invalid_group_stays_standard(self):
        self.assertNotIn("service_tier", await self.apply({}, {}))
        self.assertNotIn("service_tier", await self.apply({"speed_group": "other"}, {}))
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
/home/ec2-user/litellm-proxy/venv-1.91.1/bin/python -m unittest -v ops.litellm.test_speed_group_callback
```

Expected: import failure because `speed_group_callback.py` does not exist.

- [ ] **Step 3: Implement the minimal callback**

```python
from litellm.integrations.custom_logger import CustomLogger


class SpeedGroupCallback(CustomLogger):
    async def async_pre_call_hook(self, user_api_key_dict, cache, data, call_type):
        request_data = dict(data or {})
        if request_data.get("service_tier") not in (None, ""):
            return request_data
        metadata = getattr(user_api_key_dict, "metadata", None) or {}
        if str(metadata.get("speed_group") or "").strip().lower() == "fast":
            request_data["service_tier"] = "priority"
        return request_data


speed_group_callback = SpeedGroupCallback()
```

- [ ] **Step 4: Run hook tests and verify GREEN**

Run the Step 2 command. Expected: 4 tests pass.

- [ ] **Step 5: Commit the hook**

```bash
git add ops/litellm/speed_group_callback.py ops/litellm/test_speed_group_callback.py
git commit -m "feat(litellm): add key speed group callback"
```

### Task 2: Portal Speed-Group Domain Module

**Files:**
- Create: `key-portal/speed_groups.py`
- Create: `key-portal/test_speed_groups.py`

**Interfaces:**
- Produces: `normalize_speed_group(value) -> str`, `require_speed_group(value) -> str`, `effective_speed_group(metadata) -> str`, `canonical_litellm_key(api_key) -> str`, and `merge_speed_group_metadata(metadata, speed_group) -> dict`.

- [ ] **Step 1: Write failing pure-domain tests**

```python
class SpeedGroupDomainTests(unittest.TestCase):
    def test_missing_group_defaults_to_standard(self):
        self.assertEqual(speed_groups.normalize_speed_group(None), "standard")

    def test_invalid_group_is_rejected_for_writes(self):
        with self.assertRaises(ValueError):
            speed_groups.require_speed_group("turbo")

    def test_usr_pool_key_uses_litellm_canonical_form(self):
        self.assertEqual(speed_groups.canonical_litellm_key("usr_pool_0174_x"), "sk-usr_pool_0174_x")

    def test_metadata_merge_preserves_existing_fields(self):
        metadata = {"email": "u@zilliz.com", "model_group": "common"}
        self.assertEqual(
            speed_groups.merge_speed_group_metadata(metadata, "fast"),
            {"email": "u@zilliz.com", "model_group": "common", "speed_group": "fast"},
        )
```

- [ ] **Step 2: Run tests and verify RED**

Run:

```bash
cd key-portal && python3 -m unittest -v test_speed_groups.py
```

Expected: import failure because `speed_groups.py` does not exist.

- [ ] **Step 3: Implement the domain functions**

```python
STANDARD = "standard"
FAST = "fast"
ALLOWED = {STANDARD, FAST}


def normalize_speed_group(value):
    value = str(value or "").strip().lower()
    return value if value in ALLOWED else STANDARD


def require_speed_group(value):
    value = str(value or "").strip().lower()
    if value not in ALLOWED:
        raise ValueError("speed_group must be standard or fast")
    return value


def effective_speed_group(metadata):
    return normalize_speed_group((metadata or {}).get("speed_group"))


def canonical_litellm_key(api_key):
    value = str(api_key or "").strip()
    return "sk-" + value if value.startswith("usr_pool_") else value


def merge_speed_group_metadata(metadata, speed_group):
    return {**(metadata or {}), "speed_group": require_speed_group(speed_group)}
```

- [ ] **Step 4: Run tests and verify GREEN**

Run the Step 2 command. Expected: 4 tests pass.

- [ ] **Step 5: Commit the domain module**

```bash
git add key-portal/speed_groups.py key-portal/test_speed_groups.py
git commit -m "feat(key-portal): define key speed groups"
```

### Task 3: Authoritative Metadata Read and Self-Service Update API

**Files:**
- Modify: `key-portal/app.py`
- Modify: `key-portal/test_speed_groups.py`

**Interfaces:**
- Produces: `litellm_speed_groups_for_entries(entries) -> dict[str, dict]` where each value contains `speed_group` and `editable`.
- Produces: `POST /api/keys/speed-group` with JSON `{key, speed_group}` and response `{success, speed_group}`.

- [ ] **Step 1: Add failing tests for ownership, metadata preservation, and legacy normalization**

```python
class SpeedGroupApiTests(unittest.TestCase):
    def setUp(self):
        self.user_data = {
            "users": {
                "u@zilliz.com": {"api_keys": ["usr_pool_test"]},
                "other@zilliz.com": {"api_keys": ["sk-other"]},
            },
            "keys": {
                "usr_pool_test": {"email": "u@zilliz.com", "source": "cliproxy"},
                "sk-other": {"email": "other@zilliz.com", "source": "litellm"},
            },
        }

    def test_owner_can_switch_key_to_fast(self):
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@zilliz.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data), \
             patch.object(portal_app, "litellm_speed_groups_for_entries", return_value={"usr_pool_test": {"editable": True, "metadata": {"email": "u@zilliz.com"}, "speed_group": "standard"}}), \
             patch.object(portal_app, "update_litellm_key_metadata", return_value=(True, None)) as update:
            response = client.post("/api/keys/speed-group", json={"key": "usr_pool_test", "speed_group": "fast"})
        self.assertEqual(response.status_code, 200)
        update.assert_called_once_with("sk-usr_pool_test", {"email": "u@zilliz.com", "speed_group": "fast"})

    def test_non_owner_cannot_switch_key(self):
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@zilliz.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data), \
             patch.object(portal_app, "update_litellm_key_metadata") as update:
            response = client.post("/api/keys/speed-group", json={"key": "sk-other", "speed_group": "fast"})
        self.assertEqual(response.status_code, 403)
        update.assert_not_called()

    def test_invalid_group_returns_400(self):
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@zilliz.com", "user": {}}), \
             patch.object(portal_app, "load_user_keys", return_value=self.user_data):
            response = client.post("/api/keys/speed-group", json={"key": "usr_pool_test", "speed_group": "turbo"})
        self.assertEqual(response.status_code, 400)
```

- [ ] **Step 2: Run focused tests and verify RED**

Run:

```bash
cd key-portal && python3 -m unittest -v test_speed_groups.SpeedGroupApiTests
```

Expected: 404 because the route and helpers are not defined.

- [ ] **Step 3: Implement batched reads and metadata updates**

Add a lightweight Postgres lookup using a `VALUES` CTE containing each raw key plus the SHA-256 hashes of both raw and canonical forms:

```python
def litellm_speed_groups_for_entries(entries):
    values = []
    raw_keys = []
    for entry in entries or []:
        raw_key = str(entry.get("key") if isinstance(entry, dict) else entry or "").strip()
        if not raw_key or raw_key in raw_keys:
            continue
        raw_keys.append(raw_key)
        canonical = speed_groups.canonical_litellm_key(raw_key)
        candidates = (raw_key, _litellm_token_hash(raw_key), canonical, _litellm_token_hash(canonical))
        values.append("(" + ",".join(_sql_literal(value) for value in (raw_key, *candidates)) + ")")
    if not values:
        return {}
    sql = f"""
WITH input_keys(raw_key, candidate_1, candidate_2, candidate_3, candidate_4) AS (
    VALUES {','.join(values)}
), rows AS (
    SELECT
        i.raw_key,
        v.token IS NOT NULL AS editable,
        coalesce(v.metadata, '{{}}'::jsonb) AS metadata
    FROM input_keys i
    LEFT JOIN LATERAL (
        SELECT token, metadata
        FROM "LiteLLM_VerificationToken"
        WHERE token IN (i.candidate_1, i.candidate_2, i.candidate_3, i.candidate_4)
        LIMIT 1
    ) v ON true
)
SELECT coalesce(json_object_agg(
    raw_key,
    json_build_object('editable', editable, 'metadata', metadata)
), '{{}}'::json) FROM rows
"""
    payload = litellm_psql_json(sql, timeout=5) or {}
    for raw_key, info in payload.items():
        info["speed_group"] = speed_groups.effective_speed_group(info.get("metadata"))
    return payload
```

Implement `/key/update` calls with the canonical raw key and a fully merged metadata dictionary:

```python
def update_litellm_key_metadata(api_key, metadata):
    response = requests.post(
        f"{config.LITELLM_API_URL}/key/update",
        headers={"Authorization": f"Bearer {config.LITELLM_MASTER_KEY}"},
        json={"key": api_key, "metadata": metadata},
        timeout=30,
    )
    if response.status_code == 200:
        return True, None
    return False, f"LiteLLM key update failed: {response.status_code}"
```

The route must call `user_can_access_key`, look up current LiteLLM metadata, merge through `merge_speed_group_metadata`, and return 502 on lookup/update failure. Do not echo the key.

```python
@app.route("/api/keys/speed-group", methods=["POST"])
def update_key_speed_group_api():
    body = request.get_json(silent=True) or {}
    api_key = str(body.get("key") or "").strip()
    if not api_key:
        return jsonify({"error": "请提供 API Key"}), 400
    try:
        speed_group = speed_groups.require_speed_group(body.get("speed_group"))
    except ValueError:
        return jsonify({"error": "运行模式必须是 standard 或 fast"}), 400
    user_data = load_user_keys()
    if not user_can_access_key(api_key, user_data):
        return jsonify({"error": "不能修改其他用户的 Key"}), 403
    info = litellm_speed_groups_for_entries([api_key]).get(api_key, {})
    if not info.get("editable"):
        return jsonify({"error": "该 Key 尚未匹配到 LiteLLM，暂不能修改运行模式"}), 502
    metadata = speed_groups.merge_speed_group_metadata(info.get("metadata"), speed_group)
    canonical_key = speed_groups.canonical_litellm_key(api_key)
    success, error = update_litellm_key_metadata(canonical_key, metadata)
    if not success:
        return jsonify({"error": error or "运行模式更新失败"}), 502
    return jsonify({"success": True, "speed_group": speed_group})
```

- [ ] **Step 4: Add authoritative speed fields to both key-list APIs**

Before building rows in `/api/my-keys` and `/api/query-by-key`, call `litellm_speed_groups_for_entries`. Add:

```python
"speed_group": speed_info.get("speed_group", "standard"),
"can_change_speed": not is_synthetic_usage_key and bool(speed_info.get("editable")),
```

- [ ] **Step 5: Run focused and existing Portal tests**

```bash
cd key-portal && python3 -m unittest -v test_speed_groups.py test_usage_stats.py
```

Expected: all tests pass.

- [ ] **Step 6: Commit the backend API**

```bash
git add key-portal/app.py key-portal/test_speed_groups.py
git commit -m "feat(key-portal): let users update key speed"
```

### Task 4: New-Key and Approval Propagation

**Files:**
- Modify: `key-portal/app.py`
- Modify: `key-portal/approval.py`
- Modify: `key-portal/test_speed_groups.py`

**Interfaces:**
- `issue_litellm_key(..., speed_group="standard")`
- `assign_key_to_user(..., speed_group="standard", max_budget=None)`
- `approval.create_approval_request(..., speed_group="standard")`

- [ ] **Step 1: Add failing direct-issuance and approval tests**

Test direct and approval propagation:

```python
class SpeedGroupCreationTests(unittest.TestCase):
    def test_direct_registration_forwards_fast(self):
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@zilliz.com", "user": {"name": "User"}}), \
             patch.object(portal_app, "assign_key_to_user", return_value=("sk-created", None)) as assign:
            response = client.post("/api/register-key", json={"label": "interactive", "model_group": "common", "speed_group": "fast"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["speed_group"], "fast")
        self.assertEqual(assign.call_args.kwargs["speed_group"], "fast")

    def test_registration_defaults_to_standard(self):
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@zilliz.com", "user": {"name": "User"}}), \
             patch.object(portal_app, "assign_key_to_user", return_value=("sk-created", None)) as assign:
            response = client.post("/api/register-key", json={"label": "offline", "model_group": "common"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(assign.call_args.kwargs["speed_group"], "standard")

    def test_approval_registration_stores_fast(self):
        with portal_app.app.test_client() as client, \
             patch.object(portal_app, "current_portal_session", return_value={"email": "u@zilliz.com", "user": {"name": "User"}}), \
             patch.object(approval, "create_approval_request", return_value=("approval-id", None)) as create:
            response = client.post("/api/register-key", json={
                "label": "interactive", "model_group": "claude", "speed_group": "fast",
                "reason": "interactive work", "daily_budget": "5",
            })
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.get_json()["pending_approval"])
        self.assertEqual(create.call_args.kwargs["speed_group"], "fast")

    def test_approved_request_preserves_fast(self):
        row = {
            "id": 1, "email": "u@zilliz.com", "name": "User", "label": "interactive",
            "model_group": "claude", "daily_budget": "5", "request_payload": '{"speed_group":"fast"}',
        }
        with patch.object(portal_app, "assign_key_to_user", return_value=("sk-created", None)) as assign, \
             patch.object(approval, "_mark_request_approved", return_value=1), \
             patch.object(approval, "_notify_user_approved"):
            ok, _ = approval._on_new_key_approved(row)
        self.assertTrue(ok)
        self.assertEqual(assign.call_args.kwargs["speed_group"], "fast")
```

- [ ] **Step 2: Run focused tests and verify RED**

```bash
cd key-portal && python3 -m unittest -v test_speed_groups.SpeedGroupCreationTests
```

Expected: assertions fail because issuance and approval do not accept `speed_group`.

- [ ] **Step 3: Propagate the validated value**

In `register_key`, validate with `require_speed_group(data.get("speed_group", "standard"))`. Add it to direct issuance, API responses, and approval creation. Add it to LiteLLM generation metadata:

```python
"metadata": {
    "email": email,
    "name": name or email,
    "label": label or "默认",
    "model_group": model_group,
    "key_type": group_config["key_type"],
    "issuer": "key-portal",
    "speed_group": speed_group,
},
```

For approvals, store `{"speed_group": speed_group}` in `request_payload`; parse it in `_on_new_key_approved`, defaulting to Standard for old pending rows.

- [ ] **Step 4: Run creation and full Portal tests**

```bash
cd key-portal && python3 -m unittest -v test_speed_groups.py test_usage_stats.py
```

Expected: all tests pass.

- [ ] **Step 5: Commit creation propagation**

```bash
git add key-portal/app.py key-portal/approval.py key-portal/test_speed_groups.py
git commit -m "feat(key-portal): choose speed when creating keys"
```

### Task 5: Application and My Keys UI

**Files:**
- Create: `key-portal/static/key_speed_groups.js`
- Modify: `key-portal/static/portal_shell.css`
- Modify: `key-portal/templates/index.html`
- Modify: `key-portal/templates/my_keys.html`
- Modify: `key-portal/test_speed_groups.py`

**Interfaces:**
- Produces browser global `KeySpeedGroups.update(apiKey, speedGroup) -> Promise<object>`.

- [ ] **Step 1: Add failing template/static assertions**

Add concrete source assertions:

```python
class SpeedGroupTemplateTests(unittest.TestCase):
    def test_application_form_defaults_to_standard(self):
        html = (Path(__file__).parent / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('value="standard"', html)
        self.assertIn('value="fast"', html)
        self.assertIn('value="standard" checked', html)
        self.assertIn("离线任务、批处理和后台任务要求使用普通模式", html)

    def test_my_keys_loads_speed_controls(self):
        html = (Path(__file__).parent / "templates" / "my_keys.html").read_text(encoding="utf-8")
        self.assertIn("/static/key_speed_groups.js", html)
        self.assertIn("运行模式", html)
        self.assertIn("setKeySpeedGroup", html)
```

- [ ] **Step 2: Run template tests and verify RED**

```bash
cd key-portal && python3 -m unittest -v test_speed_groups.SpeedGroupTemplateTests
```

Expected: assertions fail because controls and assets are absent.

- [ ] **Step 3: Implement the shared browser API**

```javascript
window.KeySpeedGroups = Object.freeze({
  normalize(value) { return value === 'fast' ? 'fast' : 'standard'; },
  async update(key, speedGroup) {
    const response = await fetch('/api/keys/speed-group', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key, speed_group: speedGroup })
    });
    const payload = await response.json();
    if (!response.ok) throw new Error(payload.error || '运行模式更新失败');
    return payload;
  }
});
```

- [ ] **Step 4: Add polished controls**

Use a two-state segmented control labeled `普通模式` and `Fast 模式`. The application form defaults to Standard and displays `离线任务、批处理和后台任务要求使用普通模式`. My Keys disables non-editable controls, disables the active control during save, and restores the previous selection with an inline error on failure.

Include `speed_group` in the registration JSON body and returned local key record.

- [ ] **Step 5: Run tests and browser QA**

```bash
cd key-portal && python3 -m unittest -v test_speed_groups.py test_usage_stats.py
```

Use Playwright against a local test server at desktop `1440x1000` and mobile `390x844`. Verify no overlap, both form options fit, table/card controls remain stable, and a mocked update failure restores the old state.

- [ ] **Step 6: Commit the UI**

```bash
git add key-portal/static/key_speed_groups.js key-portal/static/portal_shell.css key-portal/templates/index.html key-portal/templates/my_keys.html key-portal/test_speed_groups.py
git commit -m "feat(key-portal): add key speed controls"
```

### Task 6: Production Reconciliation, Canary, and Rollout

**Files:**
- Runtime copy on both nodes: `/home/ec2-user/litellm-proxy/speed_group_callback.py`
- Runtime config on both nodes: `/home/ec2-user/litellm-proxy/config.yaml`
- Deploy Key Portal on node A from `/home/ec2-user/CLIProxyAPI/key-portal`

**Interfaces:**
- LiteLLM config registration: `callbacks: speed_group_callback.speed_group_callback` under `litellm_settings`.
- AWS profile: `biao-aws`, region `us-east-2`.
- Target group: `arn:aws:elasticloadbalancing:us-east-2:967519196399:targetgroup/cliproxy-tls-targets/1cd4d8f8d022ef51`.
- Node A: `i-0afbeaa90d8dc91e1`; node B: `i-08055c52390086849`.

- [ ] **Step 1: Verify the complete local test baseline**

```bash
cd key-portal && python3 -m unittest -v test_speed_groups.py test_usage_stats.py
/home/ec2-user/litellm-proxy/venv-1.91.1/bin/python -m unittest -v ops.litellm.test_speed_group_callback
go test ./...
go build -o test-output ./cmd/server && rm test-output
```

Expected: all tests pass and Go compile verification succeeds.

- [ ] **Step 2: Reconcile the eight unmatched Portal keys read-only first**

Classify each as canonical-key mismatch, obsolete Portal record, or missing LiteLLM virtual key. Repair only a key whose existing owner and active LiteLLM identity are unambiguous. Re-run the aggregate audit and require all user-editable Portal keys to match a LiteLLM token.

- [ ] **Step 3: Canary node B**

Deregister node B from the target group and wait for `unused`. Confirm `ss -ltnp 'sport = :4000'` points to the 1.91.1 `litellm-proxy.service`; stop and disable the obsolete `litellm.service` unit if it is still running so it cannot take port 4000 during restart. Copy the callback and tested LiteLLM config to B, validate module import, restart `litellm-proxy.service`, and require `/healthz` plus `/litellm/health/liveliness` to return 200. Run the request-policy matrix directly against node B, then register B and wait for `healthy`.

- [ ] **Step 4: Roll out node A**

Repeat the listener-owner check, obsolete-service cleanup, drain, copy, config validation, restart, direct-node request matrix, registration, and healthy wait for node A. Do not restart CLIProxyAPI or Nginx.

- [ ] **Step 5: Deploy and restart Key Portal**

Restart only `key-portal.service` on node A. Verify `/api/session`, `/api/my-keys`, application form rendering, and one owned-key Standard/Fast/Standard round trip.

- [ ] **Step 6: Verify through the public NLB**

Use a dedicated test key and low-token real requests to confirm:

| Key metadata | Request body | Expected |
| --- | --- | --- |
| Standard | no `service_tier` | Standard |
| Fast | no `service_tier` | `priority` |
| Fast | `service_tier=default` | Standard |
| Standard | `service_tier=priority` | `priority` |

Confirm requests hit both nodes, no new 4xx/5xx cluster appears, and SpendLogs retain the correct key/user attribution.

- [ ] **Step 7: Final verification commit**

```bash
git status --short
git log --oneline -6
```

Do not commit runtime configs, secrets, logs, backups, binaries, or unrelated dirty files.
