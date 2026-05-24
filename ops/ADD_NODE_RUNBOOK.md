# 新增 CLIProxyAPI 节点 Runbook

最后更新: 2026-05-23

本文档说明未来新增 node-c / node-d / node-e 这类工作节点时需要做什么、哪些配置可以复制、哪些必须按节点定制。

## 核心原则

1. Key Portal 只运行在 node-a。
2. 新增节点只运行 Nginx、LiteLLM、cliproxyapi 和轻量 health-agent，不运行完整 Key Portal。当前总体架构见 `ops/ARCHITECTURE.md`。
3. 新增节点的 `/`、`/admin/*`、`/socket.io/*`、Key Portal API 等入口必须反代回 node-a 的 `172.31.17.144:18080`。
4. `/v1/*` 模型请求必须走本节点自己的 LiteLLM，再走本节点自己的 cliproxyapi。
5. 认证文件目录 `~/.cli-proxy-api/` 不要自动复制；由人工登录和管理。
6. 新节点加入 NLB 前，必须确认本机业务 health-agent 返回 200，且 Nginx `/healthz` 已代理到 health-agent 并返回 200。
7. NLB 是 TCP 443 passthrough，TLS 在每个节点的 Nginx 上终止。
8. node-a 可作为开发工作区，但不要覆盖、重启或扰动 node-a 正在运行的服务；实验部署先放 node-b。

## 当前参考架构

```text
用户请求 → token.zasdas.com
  → NLB cliproxy-nlb TCP:443
    → node-a Nginx :443
    → node-b Nginx :443
    → node-c Nginx :443
```

模型请求链路:

```text
/v1/*
  → 本节点 Nginx
  → 本节点 LiteLLM 127.0.0.1:4000
  → 本节点 cliproxyapi 127.0.0.1:8317
  → 上游模型服务
```

Key Portal 链路:

```text
/、/admin/*、/socket.io/*、portal API
  → 任意节点 Nginx
  → node-a Key Portal 172.31.17.144:18080
```

## AWS 资源规格

按现有 node-b/node-c 复制即可:

- Region: `us-east-2`
- AMI: `ami-0eb6f60b2909a10f7` (`al2023 arm64`)
- Instance type: `c7g.xlarge`
- Subnet: `subnet-08a5e4e391a7304c4`
- VPC: `vpc-0f364ac1dc5cb2e11`
- Security group: `sg-0940fb5bea71786a3`
- Root volume: 200GB gp3, 3000 IOPS, 125 MB/s throughput
- Key pair: `biao-key`
- EIP: 每个新节点单独申请并绑定一个 Elastic IP

Target Group:

- Name: `cliproxy-tls-targets`
- ARN: `arn:aws:elasticloadbalancing:us-east-2:967519196399:targetgroup/cliproxy-tls-targets/1cd4d8f8d022ef51`
- Protocol: TCP
- Port: 443
- Health check: HTTPS `/healthz`, matcher `200-399`

## Security Group 要求

新节点沿用 node-b 安全组时应满足:

- TCP 443: `0.0.0.0/0`
- TCP 22: 管理员 IP + node-a 安全组
- TCP 8317: VPC 内网 `172.31.0.0/16` 或节点安全组
- Egress: `0.0.0.0/0`

注意: 如果 cliproxyapi 统一只监听 `127.0.0.1:8317`，8317 内网入站不是必须依赖项，但保留不会影响 Nginx 本机反代。

## 文件复制清单

### 可以从现有节点复制

应用目录:

- `/home/ec2-user/CLIProxyAPI/`
- `/home/ec2-user/litellm-proxy/`

Nginx 文件:

- `/etc/nginx/conf.d/cliproxyapi.conf`
- `/etc/nginx/management_allowlist.conf`
- `/etc/nginx/trusted_source_allowlist.conf`
- `/etc/nginx/cliproxy_model_external_key_gate.conf`
- `/etc/nginx/cliproxy_external_api_key_allowlist.map`
- `/etc/nginx/proxy_params`
- `/etc/nginx/ssl/token.zasdas.com/fullchain.pem`
- `/etc/nginx/ssl/token.zasdas.com/key.pem`

systemd 文件:

- `/etc/systemd/system/cliproxyapi.service`
- `/etc/systemd/system/cliproxyapi.service.d/bedrock.conf`
- `/etc/systemd/system/cliproxyapi.service.d/memory-limit.conf`
- `/etc/systemd/system/litellm-proxy.service`
- `/etc/systemd/system/litellm-proxy.service.d/root-path.conf`
- `/etc/systemd/system/litellm-proxy.service.d/memory-limit.conf`
- `/etc/systemd/system/key-portal-health.service`

Bedrock 环境文件:

- `/home/ec2-user/.claude-profiles/bedrock/aws-systemd.env`

### 不要复制

认证文件目录不要复制:

- `/home/ec2-user/.cli-proxy-api/*.json`

新节点只创建空目录:

```bash
mkdir -p /home/ec2-user/.cli-proxy-api
chmod 700 /home/ec2-user/.cli-proxy-api
```

认证文件由人工通过管理后台登录生成。

Key Portal 不要在新节点启用完整服务:

- 不复制/不启用 `/etc/systemd/system/key-portal.service`
- 不启动完整 key-portal Web 服务
- 可以复制 `key-portal/health_agent.py`、`key-portal/nlb_monitor.py` 以及少量依赖模块，供 health-agent 和离线验证使用

## CLIProxyAPI 配置要求

文件:

- `/home/ec2-user/CLIProxyAPI/config.yaml`

推荐统一:

```yaml
host: 127.0.0.1
port: 8317
auth-dir: /home/ec2-user/.cli-proxy-api
api-keys: []
```

可复制内容:

- provider 配置
- `oauth-model-alias`
- Bedrock / DeepSeek / OpenAI-compatible 配置
- payload override
- remote-management 配置

不要通过 CLIProxyAPI 再维护用户 API key allowlist；用户 key 校验由 LiteLLM 负责。

## LiteLLM 配置要求

文件:

- `/home/ec2-user/litellm-proxy/config.yaml`
- `/home/ec2-user/litellm-proxy/secrets.env`

systemd 启动建议统一:

```text
--host 127.0.0.1 --port 4000
```

模型 `api_base` 应指向本机 cliproxyapi:

```yaml
api_base: http://127.0.0.1:8317/v1
```

LiteLLM DB 是共享状态，所以用户 key、budget、model allowlist、aliases 不需要按节点单独迁移。

## Health Agent 业务健康检查

每个工作节点建议运行轻量 health-agent，而不是完整 Key Portal。代码放在仓库 `key-portal/health_agent.py`，systemd unit 为 `/etc/systemd/system/key-portal-health.service`。

职责:

- 监听本机 `127.0.0.1:18081`。
- 查询本节点 cliproxyapi `/healthz`。
- 查询本节点 management `/v0/management/auth-files`。
- 确认可用认证文件数量不少于 `HEALTH_AGENT_MIN_USABLE_AUTH_FILES`。
- 可选用 `HEALTH_AGENT_REQUIRE_PROVIDERS=codex,claude` 要求指定 provider 至少有一个可用 auth file。
- 返回 200 表示业务可承载流量，返回 503 表示本节点不应承载新流量。

推荐 unit:

```ini
[Unit]
Description=Key Portal Health Agent for NLB readiness
After=network-online.target cliproxyapi.service
Wants=network-online.target

[Service]
Type=simple
User=ec2-user
WorkingDirectory=/home/ec2-user/CLIProxyAPI/key-portal
Environment=HEALTH_AGENT_HOST=127.0.0.1
Environment=HEALTH_AGENT_PORT=18081
Environment=CLIPROXY_API_URL=http://127.0.0.1:8317
Environment=HEALTH_AGENT_MIN_USABLE_AUTH_FILES=1
ExecStart=/usr/bin/python3 /home/ec2-user/CLIProxyAPI/key-portal/health_agent.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```

历史 node-b 如果 cliproxyapi 仍监听 `172.31.26.28:8317`，则 `CLIPROXY_API_URL` 应按节点改成对应内网地址。

当前 Nginx `/healthz` 应代理到 `127.0.0.1:18081/healthz`，让 NLB 按 health-agent 的业务健康结果摘除/恢复节点。变更前后都要用本机命令验证:

```bash
curl -sS -w "http=%{http_code}\n" http://127.0.0.1:18081/healthz
curl -k -H 'Host: token.zasdas.com' -sS -w "http=%{http_code}\n" https://127.0.0.1/healthz
```

## Key Portal / LiteLLM 监控外迁

CLIProxyAPI core 里的用户请求监控 middleware 应逐步迁出，不再在 core 里硬编码 key 或记录完整请求/响应体。外围替代方案:

- Key Portal 通过 LiteLLM `LiteLLM_SpendLogs` 查询指定用户/key 最近请求元数据。
- API: `GET /api/user-monitor/recent-requests?api_key=<key>&hours=24&limit=100`
- 对账 API: `GET /api/user-monitor/reconcile?api_key=<key>&hours=24&limit=500`
- 旧 `monitor-logs/*.log` 只解析元数据，跳过 `REQUEST BODY` 和 `RESPONSE` 内容。
- 返回 key 必须 mask，不输出完整用户 key。

依赖:

```bash
python3 -m venv /home/ec2-user/CLIProxyAPI/key-portal/.venv
/home/ec2-user/CLIProxyAPI/key-portal/.venv/bin/pip install -r /home/ec2-user/CLIProxyAPI/key-portal/requirements.txt
```

`litellm_psql_json()` 优先使用 `psql`，如果节点没有 `psql`，会 fallback 到 `psycopg`。工作节点可复用 `/home/ec2-user/litellm-proxy/secrets.env` 中的 `DATABASE_URL` 做只读验证。

node-b 验证过的最小检查:

```bash
set -a
. /home/ec2-user/litellm-proxy/secrets.env
set +a
PYTHONPATH=/home/ec2-user/CLIProxyAPI/key-portal \
  /home/ec2-user/CLIProxyAPI/key-portal/.venv/bin/python - <<'PY'
import app
print({"db_query": len(app.litellm_recent_requests(api_key="__nonexistent__", hours=1, limit=1))})
print({"monitor_log": len(app.monitor_log_recent_entries("__nonexistent__", hours=1, limit=1))})
PY
```

完整 Key Portal 仍只在 node-a 对外运行；工作节点上的 venv 只是为了验证 health/monitoring 代码和未来推广，不要启用 `key-portal.service`。

## Nginx 配置要求

### `/v1/*`

所有节点一致，走本机 LiteLLM:

```nginx
proxy_pass http://127.0.0.1:4000/litellm$request_uri;
```

### `/healthz`

必须返回本节点 health-agent 的业务健康检查。health-agent 再检查本节点 cliproxyapi、LiteLLM 和可用认证文件。

```nginx
location = /healthz { proxy_pass http://127.0.0.1:18081/healthz; include /etc/nginx/proxy_params; }
```

如果 health-agent 返回 503，NLB 应停止把新流量分配到该节点。不要把 `/healthz` 指向其它节点，否则会掩盖本节点故障。

### Key Portal 路由

node-a:

```nginx
proxy_pass http://127.0.0.1:18080;
```

node-b/node-c/node-d:

```nginx
proxy_pass http://172.31.17.144:18080;
```

需要检查所有 Key Portal 相关 location，包括:

- `/`
- `/socket.io/`
- `/admin/*`
- `/api/usage-summary`
- 其它 Key Portal 页面/API

不能让非 node-a 节点出现 `proxy_pass http://127.0.0.1:18080`，否则 NLB 打到该节点时首页会 502。

### 稳定管理后台入口

每个节点都应支持稳定入口:

```text
/a/management.html -> node-a
/b/management.html -> node-b
/c/management.html -> node-c
/d/management.html -> node-d
```

新增 node-d 时，需要在所有节点 Nginx 里增加:

```text
/d/management.html
/d/v0/management
/d/v0/management/
```

如果跨节点走 HTTPS 反代到目标节点 Nginx，需要设置 SNI:

```nginx
proxy_ssl_verify off;
proxy_ssl_server_name on;
proxy_ssl_name token.zasdas.com;
```

并保证 Host header 不被错误覆盖。若使用公共证书但目标是内网 IP，这一点很重要。

## 新增节点步骤

### 1. 创建 EC2 + EIP

创建同规格实例，绑定新 EIP，记录:

- instance id
- private IP
- public EIP

### 2. 配置 SSH

如果本机没有 EC2 key pair 私钥，可以用 EC2 Instance Connect 临时注入集群 SSH 公钥，再写入 `~/.ssh/authorized_keys`。

### 3. 安装基础包

```bash
sudo dnf install -y nginx python3-pip git rsync tar gzip findutils jq shadow-utils openssl
```

### 4. 同步应用和配置

同步应用目录、LiteLLM 目录、Nginx 配置、SSL 证书、systemd unit、Bedrock env。

不要同步认证文件。

### 5. 安装 LiteLLM 依赖

如果没有 requirements 文件，可按当前版本安装:

```bash
python3 -m venv /home/ec2-user/litellm-proxy/venv
/home/ec2-user/litellm-proxy/venv/bin/pip install litellm[proxy]==1.83.9 litellm-enterprise==0.1.37 litellm-proxy-extras==0.4.66
```

版本应以现有节点 `pip freeze` 为准。

### 6. 节点个性化配置

至少检查并调整:

- Nginx `server_name` 中的 EIP
- `/healthz` proxy 目标
- `/` 和 portal routes 是否回源到 node-a
- 新增 `/d/management.html` 等稳定管理入口
- 本节点自身 `/management.html` 是否打本机 cliproxyapi
- cliproxyapi 是否监听预期地址

### 7. 启动服务

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now cliproxyapi litellm-proxy nginx key-portal-health
sudo nginx -t
```

不要启动完整 key-portal Web 服务。

### 8. 加入 NLB

```bash
aws elbv2 register-targets \
  --region us-east-2 \
  --target-group-arn arn:aws:elasticloadbalancing:us-east-2:967519196399:targetgroup/cliproxy-tls-targets/1cd4d8f8d022ef51 \
  --targets Id=<INSTANCE_ID>,Port=443
```

等待健康:

```bash
aws elbv2 wait target-in-service \
  --region us-east-2 \
  --target-group-arn arn:aws:elasticloadbalancing:us-east-2:967519196399:targetgroup/cliproxy-tls-targets/1cd4d8f8d022ef51 \
  --targets Id=<INSTANCE_ID>,Port=443
```

## 验证清单

新增节点加入 NLB 前:

```bash
curl -k -H 'Host: token.zasdas.com' https://127.0.0.1/healthz
curl -k -H 'Host: token.zasdas.com' https://127.0.0.1/
curl -k -H 'Host: token.zasdas.com' https://127.0.0.1/<node-letter>/management.html
curl http://127.0.0.1:4000/health/liveliness
curl http://127.0.0.1:8317/healthz
curl http://127.0.0.1:18081/healthz
```

从现有节点检查新节点 Key Portal 回源:

```bash
curl http://172.31.17.144:18080/
```

加入 NLB 后:

```bash
curl -k https://token.zasdas.com/
curl -k https://token.zasdas.com/a/management.html
curl -k https://token.zasdas.com/b/management.html
curl -k https://token.zasdas.com/c/management.html
curl -k https://token.zasdas.com/d/management.html
```

重复访问首页至少 20 次，确认没有间歇性 502:

```bash
for i in $(seq 1 20); do
  curl -sS -k -o /tmp/root -w "try=$i http=%{http_code} size=%{size_download}\n" https://token.zasdas.com/
done
```

检查 NLB:

```bash
aws elbv2 describe-target-health \
  --region us-east-2 \
  --target-group-arn arn:aws:elasticloadbalancing:us-east-2:967519196399:targetgroup/cliproxy-tls-targets/1cd4d8f8d022ef51
```

所有目标必须是 `healthy`。

## 常见故障

### 首页间歇性 502

通常是某个非 node-a 节点的 portal route 仍指向:

```nginx
proxy_pass http://127.0.0.1:18080;
```

修为:

```nginx
proxy_pass http://172.31.17.144:18080;
```

### NLB target unhealthy

检查该节点:

```bash
curl -k -H 'Host: token.zasdas.com' https://127.0.0.1/healthz
```

如果本机 502，检查 `/healthz` 是否打错 cliproxyapi 地址。

### `/c/management.html` 或 `/d/management.html` 400/403

常见原因:

- 跨节点 HTTPS 反代没有设置 SNI
- Host header 被覆盖顺序错误
- management allowlist 未允许来源
- 目标节点 Nginx 没有正确处理 Host `token.zasdas.com`

跨节点 HTTPS 反代建议加:

```nginx
proxy_ssl_verify off;
proxy_ssl_server_name on;
proxy_ssl_name token.zasdas.com;
```

## 禁止事项

- 不要复制 `~/.cli-proxy-api/*.json` 到新节点。
- 不要自动移动、重命名、删除、恢复认证文件。
- 不要在 node-b/node-c/node-d 启动完整 Key Portal Web 服务。
- 不要把非 node-a 的 Key Portal route 指到本机 `127.0.0.1:18080`。
- 不要在未通过 `/healthz` 和 `127.0.0.1:18081/healthz` 前把节点加入 NLB。
- 不要把 health-agent 接入 Nginx/NLB，除非已经完成单独评审和灰度验证。
