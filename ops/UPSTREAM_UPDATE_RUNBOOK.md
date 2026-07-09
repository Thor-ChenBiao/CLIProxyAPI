# CLIProxyAPI upstream update runbook

_Last updated: 2026-06-01_

## Current baseline

| Item | Value |
|---|---|
| Upstream remote | `origin` = `https://github.com/router-for-me/CLIProxyAPI.git` |
| Private remote | `thor` = `https://github.com/Thor-ChenBiao/CLIProxyAPI.git` |
| Current upstream core | `origin/main` at `7d9980e8`, tag `v7.1.29` |
| Current local upgrade branch | `upgrade/v7.1.29-clean-overlay-20260529T141850Z` |
| Core policy after this update | Keep CLIProxyAPI core identical to `origin/main`; keep local changes in `key-portal/` and `ops/` only |
| Current active runtime nodes | node-a, node-b |
| Historical 2026-05-29 rollout nodes | node-a, node-b, node-c |

The important invariant is:

```bash
git diff --name-only origin/main -- . ':(exclude)key-portal/**' ':(exclude)ops/**'
```

Expected output: nothing. If it prints files, local core has drifted from upstream and should be reviewed before upgrading.

## What changed in the 2026-05-29 update

- Rebased the working tree conceptually onto upstream `v7.1.29`.
- Dropped old local core patches, including old in-memory usage statistics patches, target request monitor, Bedrock executor patch, API key permission patch, and related compatibility code.
- Preserved the local `key-portal/` and `ops/` overlay.
- Updated Key Portal usage compatibility from the removed `/v0/management/usage-statistics` endpoints to upstream `/v0/management/usage-queue`.
- Kept LiteLLM Postgres spend logs as the preferred persisted usage source.

## Runtime rollout completed on 2026-05-29

| Node | Result | Backup binary |
|---|---|---|
| node-a | switched and healthy | `/home/ec2-user/CLIProxyAPI/cliproxyapi.bak-pre-v7129-switch-20260529T143627Z` |
| node-b | switched and healthy | `/home/ec2-user/CLIProxyAPI/cliproxyapi.bak-pre-v7129-switch-20260529T144919Z` |
| node-c | switched and healthy | `/home/ec2-user/CLIProxyAPI/cliproxyapi.bak-pre-v7129-switch-20260529T144920Z` |

New binary checksum deployed on active nodes:

```text
09beab685dc831196cd6859f9c673549a2160ca459b46c0d52d08a843f378631  cliproxyapi
```

Old binary checksum before this rollout:

```text
3a1123bbe3f2f2a67e8270ea89d30153036137f831c45bb462aaadd41ee31b0f  cliproxyapi.bak-pre-v7129-switch-*
```

## Validation commands

### Repository/core validation

```bash
git fetch origin --tags --prune
git describe --tags --always origin/main
git rev-parse --short HEAD
git rev-parse --short origin/main
git diff --name-only origin/main -- . ':(exclude)key-portal/**' ':(exclude)ops/**'
```

Expected after a clean overlay update:

- `HEAD` equals `origin/main` before overlay commit, or the only committed delta is `key-portal/` and `ops/`.
- The core diff command prints nothing.

### Build and tests

```bash
python3 -m py_compile key-portal/app.py key-portal/config.py key-portal/snapshot.py key-portal/usage_sync.py key-portal/portal_scheduler.py key-portal/status_events.py
PYTHONPATH=key-portal python3 -m unittest key-portal/test_usage_stats.py
go test ./...
go build -o /tmp/cliproxyapi-next-check ./cmd/server
```

### Isolated binary smoke test

Use a temporary config, empty auth dir, and unused local port. Do not point this smoke test at the production auth directory.

```bash
SMOKE_DIR=/tmp/cliproxy-smoke
PORT=18317
rm -rf "$SMOKE_DIR"
mkdir -p "$SMOKE_DIR/auth"
cat > "$SMOKE_DIR/config.yaml" <<EOF
host: "127.0.0.1"
port: $PORT
remote-management:
  allow-remote: false
  secret-key: "smoke-management-key"
  disable-control-panel: true
auth-dir: "$SMOKE_DIR/auth"
api-keys:
  - "smoke-api-key"
debug: false
logging-to-file: false
usage-statistics-enabled: true
redis-usage-queue-retention-seconds: 60
EOF
/tmp/cliproxyapi-next-check -config "$SMOKE_DIR/config.yaml" -local-model
```

In another shell:

```bash
curl -fsS "http://127.0.0.1:$PORT/healthz"
curl -sS -H 'Authorization: Bearer smoke-api-key' "http://127.0.0.1:$PORT/v1/models"
curl -sS -H 'X-Management-Key: smoke-management-key' "http://127.0.0.1:$PORT/v0/management/usage-queue?count=1"
```

## Post-upgrade regression test suite

Run this suite after every upstream rebase/merge and before declaring the runtime upgrade complete. Keep all tests low-volume; never print full API keys in logs or chat.

### 1. Core and overlay invariants

| Test | Command | Expected |
|---|---|---|
| Upstream core is clean | `git diff --name-only origin/main -- . ':(exclude)key-portal/**' ':(exclude)ops/**'` | no output |
| Key Portal imports | `python3 -m py_compile key-portal/app.py key-portal/config.py key-portal/auth_stats_service.py key-portal/snapshot.py key-portal/usage_sync.py key-portal/status_events.py` | exit 0 |
| Key Portal unit tests | `PYTHONPATH=key-portal python3 -m unittest key-portal/test_usage_stats.py` | all pass |
| Go tests/build | `go test ./... && go build -o /tmp/cliproxyapi-next-check ./cmd/server` | exit 0 |

### 2. Runtime health checks

Run on each active node after rollout. As of 2026-06-01 the active set is node-a, node-b; node-c is retained below only as historical 2026-05-29 rollout context.

| Test | node-a command | node-b command pattern | Expected |
|---|---|---|---|
| Service active | `systemctl is-active cliproxyapi.service` | `ssh -i ~/.ssh/cluster-key ec2-user@172.31.26.28 'systemctl is-active cliproxyapi.service'` | `active` |
| Health endpoint | `curl -fsS http://127.0.0.1:8317/healthz` | `ssh -i ~/.ssh/cluster-key ec2-user@172.31.26.28 'curl -fsS http://127.0.0.1:8317/healthz'` | HTTP 200 / healthy body |
| Usage queue present | `curl -sS -H 'X-Management-Key: admin123' 'http://127.0.0.1:8317/v0/management/usage-queue?count=1'` | same via SSH | HTTP 200 JSON array |
| Auth-file API reachable | `curl -sS -H 'X-Management-Key: admin123' 'http://127.0.0.1:8317/v0/management/auth-files'` | same via SSH | HTTP 200 JSON with `files` |

Do not inspect or modify auth files on disk; only use management APIs.

### 3. Key Portal regression checks

| Test | Command / page | Expected |
|---|---|---|
| Portal process active | `systemctl is-active key-portal.service` | `active` |
| Admin auth-stats page loads | `https://token.zasdas.com/admin/auth-stats` | page renders without node 404 errors |
| Auth-stats API stable | Call `/api/auth-stats` twice within 15 seconds while authenticated | second call should use cache/keep stable counters, not consume a fresh destructive queue sample |
| Node traffic windows | Auth-stats “按节点看流量” | 1h/5h/24h/7d should be monotonic by window size after enough queue history accumulates; do not accept identical tiny counts caused only by one queue read |
| Quota estimate guard | Auth-stats “今日预估额度消耗” | must not show huge percentages from partial queue history; show sample/history-insufficient state until local history covers enough provider window |
| Usage summary | `/api/usage-summary` while authenticated | `summary_sources` should prefer `litellm_spendlogs` when LiteLLM PG is available |

### 4. Public model-routing matrix

Run from the formal public entrypoint, not from a private localhost port:

```text
https://token.zasdas.com/v1/...
```

Use low-token prompts, mask keys in logs, and record only key type, endpoint, model, HTTP status, and error type.

| Key type | Endpoint | Model | stream | Expected |
|---|---|---|---:|---|
| common / GPT | `/v1/chat/completions` | `gpt-5.5` | false | 200 |
| common / GPT | `/v1/chat/completions` | `claude-opus-4-6` | false | 200; GPT-backed Claude-compatible route |
| common / GPT | `/v1/chat/completions` | `bedrock-claude-opus-4-6` | false | 401/403 `key_model_access_denied` |
| common / GPT | `/v1/chat/completions` | `deepseek-chat` | false | 401/403 `key_model_access_denied` |
| Claude | `/v1/chat/completions` | `claude-opus-4-6` | false | 200; real Claude/Bedrock route |
| Claude | `/v1/chat/completions` | `claude-sonnet-4-6` | false | 200; real Claude/Bedrock route |
| Claude | `/v1/chat/completions` | `bedrock-claude-opus-4-6` | false | 401/403 `key_model_access_denied`; internal model name must not be user-facing |
| Claude | `/v1/chat/completions` | `claude-opus-4-7` | false | 401/403/unsupported until Bedrock actually supports it; must not silently downgrade to 4.6 |
| DeepSeek | `/v1/chat/completions` | `deepseek-chat` | false | 200 |

### 5. Bedrock regression matrix

Claude-key Bedrock regression must cover OpenAI Chat Completions and Anthropic Messages, both streaming and non-streaming.

| Endpoint | Model | stream | Expected |
|---|---|---:|---|
| `/v1/chat/completions` | `claude-opus-4-6` | false | 200 |
| `/v1/chat/completions` | `claude-opus-4-6` | true | 200, valid SSE stream |
| `/v1/messages?beta=true` | `claude-opus-4-6` | false | 200 |
| `/v1/messages?beta=true` | `claude-opus-4-6` | true | 200, valid Anthropic Messages SSE stream |

Recommended minimal payloads:

```json
{"model":"claude-opus-4-6","messages":[{"role":"user","content":"Return exactly: ok"}],"max_tokens":8,"stream":false}
```

For `/v1/messages?beta=true`, use the same message structure and toggle `stream` for the streaming case.

### 6. Known follow-up from 2026-05-29 regression run

The 2026-05-29 Bedrock run passed the main Bedrock path but exposed two policy regressions:

| Case | Expected | Observed on 2026-05-29 |
|---|---|---|
| Claude key requesting `bedrock-claude-opus-4-6` directly | 401/403 | 200 |
| Claude key requesting `claude-opus-4-7` | 401/403/unsupported | 200 |

Treat these as failures in future upgrade validation until the model allowlist/alias policy is tightened or the architecture document is intentionally updated.

## Next upstream update procedure

1. Fetch upstream:

   ```bash
   git fetch origin --tags --prune
   git describe --tags --always origin/main
   ```

2. Create a fresh update branch from upstream:

   ```bash
   TS=$(date -u +%Y%m%dT%H%M%SZ)
   git switch -C "upgrade/next-clean-overlay-$TS" origin/main
   ```

3. Restore only local overlay from the current private branch or backup branch:

   ```bash
   git restore --source=<previous-overlay-branch> --staged --worktree -- key-portal ops
   ```

4. Confirm no core drift:

   ```bash
   git diff --name-only origin/main -- . ':(exclude)key-portal/**' ':(exclude)ops/**'
   ```

5. If upstream changed management usage APIs again, update Key Portal in `key-portal/app.py` and tests in `key-portal/test_usage_stats.py`.

6. Run validations from the previous section.

7. Build the deploy binary:

   ```bash
   go build -o /tmp/cliproxyapi-next-check ./cmd/server
   sha256sum /tmp/cliproxyapi-next-check
   ```

8. Roll out node by node: node-a first, then node-b. If a future node-c/new node is deliberately reactivated, add it to the rollout set only after it is back in the active architecture document.

## Node rollout commands

### node-a

```bash
TS=$(date -u +%Y%m%dT%H%M%SZ)
BACKUP="/home/ec2-user/CLIProxyAPI/cliproxyapi.bak-pre-next-switch-$TS"
cp -a /home/ec2-user/CLIProxyAPI/cliproxyapi "$BACKUP"
install -m 0755 /tmp/cliproxyapi-next-check /home/ec2-user/CLIProxyAPI/cliproxyapi
sudo systemctl restart cliproxyapi.service
curl -fsS http://127.0.0.1:8317/healthz
curl -sS -H 'X-Management-Key: admin123' 'http://127.0.0.1:8317/v0/management/usage-queue?count=1'
```

### node-b

```bash
KEY=~/.ssh/cluster-key
for item in node-b:172.31.26.28; do
  name=${item%%:*}
  host=${item#*:}
  TS=$(date -u +%Y%m%dT%H%M%SZ)
  remote_tmp="/tmp/cliproxyapi-next-check-$TS"
  scp -i "$KEY" /tmp/cliproxyapi-next-check "ec2-user@$host:$remote_tmp"
  ssh -i "$KEY" "ec2-user@$host" "set -euo pipefail
    BACKUP=\"/home/ec2-user/CLIProxyAPI/cliproxyapi.bak-pre-next-switch-$TS\"
    cp -a /home/ec2-user/CLIProxyAPI/cliproxyapi \"\$BACKUP\"
    install -m 0755 '$remote_tmp' /home/ec2-user/CLIProxyAPI/cliproxyapi
    sudo systemctl restart cliproxyapi.service
    curl -fsS http://127.0.0.1:8317/healthz
    systemctl is-active cliproxyapi.service
  "
done
```

## Rollback

Use the backup binary from the affected node.

```bash
sudo systemctl stop cliproxyapi.service
cp -a /home/ec2-user/CLIProxyAPI/cliproxyapi.bak-pre-v7129-switch-YYYYMMDDTHHMMSSZ /home/ec2-user/CLIProxyAPI/cliproxyapi
sudo systemctl start cliproxyapi.service
curl -fsS http://127.0.0.1:8317/healthz
```

For node-b, run the same command over SSH with `~/.ssh/cluster-key`. Add future reactivated nodes only after they are back in the active architecture document.

## Notes

- Do not edit or test auth files directly. Use management APIs only.
- Avoid changing node-a runtime services except during an explicit rollout window.
- Key Portal should not call upstream provider quota APIs directly; it may call CLIProxyAPI management APIs and LiteLLM Postgres.
- The active production node set is node-a, node-b. Retired or reserved nodes such as node-c/node-d should not appear in `CLIPROXY_NODES_JSON` unless deliberately reactivated.
