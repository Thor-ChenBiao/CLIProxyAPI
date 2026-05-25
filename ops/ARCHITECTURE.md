# CLIProxyAPI 集群架构

最后更新: 2026-05-24

## 当前结论

`token.zasdas.com` 的正式模型流量已经是 NLB + 多节点本地链路:

```text
用户请求
  → token.zasdas.com CNAME
  → NLB cliproxy-nlb TCP:443 passthrough
  → 命中的工作节点 Nginx :443 终止 TLS
  → 本节点 LiteLLM 127.0.0.1:4000
  → 本节点 cliproxyapi 127.0.0.1:8317
  → 上游模型服务
```

Key Portal 仍是单 active 服务，只在 node-a 运行；其它节点的门户页面入口应回源到 node-a 的 `172.31.17.144:18080`。

## 节点信息

| 节点 | 实例ID | 实例类型 | IP (EIP) | 内网IP | 当前角色 | 健康检查观测 |
|------|--------|---------|----------|--------|----------|--------------|
| node-a (biao-go-server) | i-0afbeaa90d8dc91e1 | c7g.xlarge (4vCPU/8GB) | 3.149.220.73 | 172.31.17.144 | Nginx + LiteLLM + cliproxyapi + Key Portal + health-agent | `https://127.0.0.1/healthz`、health-agent、LiteLLM、cliproxyapi 均返回 200 |
| node-b (cliproxy-node-b) | i-08055c52390086849 | c7g.large (2vCPU/4GB) | 3.150.54.188 | 172.31.26.28 | Nginx + LiteLLM + cliproxyapi + health-agent | `https://172.31.26.28/healthz` 返回 200 |
| node-c (cliproxy-node-c) | i-07d065661a87679df | c7g.large (2vCPU/4GB) | 18.189.167.58 | 172.31.16.7 | Nginx + LiteLLM + cliproxyapi + health-agent | `https://172.31.16.7/healthz` 返回 200 |
| node-d (cliproxy-node-d) | i-0fd32ef2df7d69857 | c7g.large (2vCPU/4GB) | 3.19.6.233 | 172.31.24.86 | Nginx + LiteLLM + cliproxyapi + health-agent | `https://172.31.24.86/healthz` 返回 200 |
| node-e | 待核对 | 待核对 | 待核对 | 172.31.29.243 | 代码和 Nginx 管理入口中有预留配置 | 2026-05-24 从 node-a curl `/healthz` 不可达 |
| node-f | 待核对 | 待核对 | 待核对 | 172.31.25.74 | 代码和 Nginx 管理入口中有预留配置 | 2026-05-24 从 node-a curl `/healthz` 不可达 |

区域: `us-east-2`，VPC: `vpc-0f364ac1dc5cb2e11`，子网: `subnet-08a5e4e391a7304c4`。

当前 IAM 用户无法执行 `ec2:DescribeInstances` 和 `elasticloadbalancing:DescribeTargetHealth`，所以上表的 EC2/NLB target membership 以仓库记录、运行中 Nginx 配置和节点 healthz 探测为准；如果要做正式扩缩容，需要用有权限的 AWS profile 复核。

## 流量架构

```text
用户请求 → token.zasdas.com
    │
    ▼
┌──────────────────────────────────────┐
│  NLB: cliproxy-nlb                   │
│  DNS: cliproxy-nlb-873bf6ea679ef9dc  │
│       .elb.us-east-2.amazonaws.com   │
│  Target Group: cliproxy-tls-targets  │
│  协议: TCP:443 passthrough            │
│  健康检查: HTTPS /healthz            │
└──────┬───────────────┬───────────────┬───────────────┘
       │               │               │
       ▼               ▼               ▼
   node-a Nginx    node-b Nginx    node-c/d Nginx
   :443 TLS        :443 TLS        :443 TLS
       │               │               │
       ├── /v1/* ──────┼── /v1/* ──────┤
       │               │               │
       ▼               ▼               ▼
   local LiteLLM    local LiteLLM   local LiteLLM
   127.0.0.1:4000  127.0.0.1:4000  127.0.0.1:4000
       │               │               │
       ▼               ▼               ▼
   local cliproxyapi local cliproxyapi local cliproxyapi
   127.0.0.1:8317  127.0.0.1:8317  127.0.0.1:8317
```

### Nginx 路由边界

node-a 当前线上 Nginx 配置的关键路由:

| 路径 | 目标 | 说明 |
|------|------|------|
| `/v1/*` | `http://127.0.0.1:4000/litellm$request_uri` | 正式模型入口，先走 LiteLLM，再由 LiteLLM 转发到 cliproxyapi |
| `/v1beta/*`, `/v1internal*` | `cliproxy_api_backend` → 本机 `127.0.0.1:8317` | 兼容/内部 proxy 路径 |
| `/v0/management*`, `/management.html`, `/keep-alive` | 本机 `127.0.0.1:8317` | cliproxyapi 管理和维护入口，受管理 allowlist 保护 |
| `/healthz`, `/internal-healthz` | 本机 `127.0.0.1:18081/healthz` | health-agent 业务健康检查，供 NLB 和人工检查使用 |
| `/litellm/*` | 本机 `127.0.0.1:4000` | LiteLLM admin UI/API，受管理 allowlist 保护 |
| `/api/feishu/approval-callback` | 本机 `127.0.0.1:18080` | 飞书审批回调 |
| `/` 和 Key Portal 页面/API | node-a 本机 `127.0.0.1:18080`；工作节点应回源 `172.31.17.144:18080` | Key Portal Web/API |

Nginx 仍负责来源 IP / 网络层访问控制、管理入口 allowlist，以及 `usr_pool_*` 到 `sk-usr_pool_*` 的兼容 rewrite；用户 key 的业务校验不在 Nginx 完成。

## 服务说明

| 服务 | 端口 | 部署节点 | 当前 systemd / 入口 | 说明 |
|------|------|---------|---------------------|------|
| Nginx | `0.0.0.0:443`, `[::]:443` | node-a/b/c/d | `nginx.service` | TLS 终止、路径路由、来源访问控制 |
| LiteLLM | `127.0.0.1:4000` | node-a/b/c/d | `litellm-proxy.service` → `/home/ec2-user/litellm-proxy/venv/bin/litellm --config /home/ec2-user/litellm-proxy/config.yaml --host 127.0.0.1 --port 4000 --telemetry False` | 用户 API key 校验、模型权限、预算、限流、审计和 SpendLogs |
| cliproxyapi | `127.0.0.1:8317` | node-a/b/c/d | `cliproxyapi.service` → `/home/ec2-user/CLIProxyAPI/cliproxyapi` | Go 代理核心，上游适配、OAuth/provider/auth-file 管理，不再作为用户 key allowlist 的唯一入口 |
| Key Portal | `0.0.0.0:18080` | 仅 node-a active | `key-portal.service` → `/usr/bin/python3 /home/ec2-user/CLIProxyAPI/key-portal/app.py` | 用户密钥管理、审批、用量面板、运维页面 |
| Key Portal health-agent | `127.0.0.1:18081` | node-a/b/c/d | `key-portal-health.service` → `/usr/bin/python3 /home/ec2-user/CLIProxyAPI/key-portal/health_agent.py` | NLB-facing 业务健康检查 |

## 认证与授权边界

- Nginx: 网络来源控制、管理路径 allowlist、旧 key 兼容 rewrite。
- LiteLLM: `/v1/*` 用户 API key 的主要校验点，负责 key 是否存在、是否 blocked、模型权限、预算、限流和审计写入。
- cliproxyapi: 模型代理和上游适配；用户 key 校验应通过 Nginx/LiteLLM 边界完成，不要重新在 proxy core 里维护用户 key allowlist。
- Key Portal: 管理用户 key、审批、预算和可视化；统计应读 LiteLLM/cliproxyapi 已产生的数据，不主动轮询上游 provider quota API。

## Health check

NLB health check 是 HTTPS `/healthz`。当前 Nginx `/healthz` 已代理到本机 `127.0.0.1:18081/healthz` 的 health-agent。

health-agent 检查内容:

1. 本机 cliproxyapi `/healthz`。
2. 本机 LiteLLM `/health/liveliness`。
3. 本机 cliproxyapi management `/v0/management/auth-files`。
4. 可用 auth file 数量不少于 `HEALTH_AGENT_MIN_USABLE_AUTH_FILES`。
5. 如果设置 `HEALTH_AGENT_REQUIRE_PROVIDERS`，指定 provider 至少有一个可用 auth file。

返回 200 表示节点可承载新流量；返回 503 表示节点应被 NLB 摘除或人工检查。

## Key Portal 集群视图

`key-portal/app.py` 默认管理节点列表:

```text
node-a http://127.0.0.1:8317
node-b http://172.31.26.28:8317
node-c https://172.31.16.7
node-d https://172.31.24.86
node-e https://172.31.29.243
node-f https://172.31.25.74
```

其中 node-e/node-f 是配置中预留或候选节点；2026-05-24 从 node-a 直接探测 `/healthz` 不可达，不能在未验证前视为 NLB active target。

## 模型路由规则

用户侧使用统一公开模型名，特别是 Claude 兼容名应保持为 `claude-*`，例如 `claude-opus-4-7`。不要要求用户为了选择真实 Bedrock Claude 而改用 `bedrock-claude-*` 或 `bedrock/...` 这类内部路由名；真实后端必须由 API key 的模型组决定。

关键规则:

- common / GPT key 请求 `claude-*` 公开模型名时，应走 GPT-backed Claude 兼容路由，不走 Bedrock。
- Claude key 请求同一个 `claude-*` 公开模型名时，应走真实 Claude / Bedrock 路由，并消耗 Claude key 的总额度。
- DeepSeek key 请求 `deepseek-*` 公开模型名时，应走 DeepSeek provider，并消耗 DeepSeek key 的总额度。
- `bedrock-claude-*`、`bedrock/...` 等名称是内部实现细节，不应作为用户侧模型选择规则，也不应成为区分 common key 和 Claude key 的用户操作方式。

实现方式:

- common key 的 LiteLLM allowlist 包含 GPT 模型和 GPT-backed 的 `claude-*` 公开模型名，不设置 key-specific aliases；请求到达 cliproxyapi 后由 cliproxyapi 的 `oauth-model-alias` 路由到 GPT/Codex provider。
- Claude key 的 LiteLLM allowlist 只暴露 `claude-*` 公开模型名，同时在 key 记录上设置 key-specific `aliases`，例如 `claude-opus-4-6` → `bedrock-claude-opus-4-6`；LiteLLM 在转发前按 key 改写到内部 Bedrock 模型名。
- LiteLLM key 记录存储在共享数据库中，现有 key 的 allowlist / aliases 迁移只需对 LiteLLM 数据库执行一次，各节点都会生效。

回归测试必须从正式入口 `https://token.zasdas.com/v1/...` 发起，模拟用户行为，而不是只打本机端口。测试时禁止在用户输出中打印完整 key；只展示 key 类型、模型名、HTTP 状态和必要错误类型。

基础路由矩阵:

| Key 类型 | 用户请求 model | 期望结果 | 目的 |
|---|---|---|---|
| common / GPT key | `gpt-5.5` | 200 | common key 正常 GPT 通路 |
| common / GPT key | `claude-opus-4-6` | 200，返回公开模型名仍为 `claude-opus-4-6` | common key 使用 GPT-backed Claude 兼容路由 |
| common / GPT key | `bedrock-claude-opus-4-6` | 401 / 403 `key_model_access_denied` | common key 不能访问内部 Bedrock 模型名 |
| common / GPT key | `deepseek-chat` | 401 / 403 `key_model_access_denied` | common key 不能访问 DeepSeek |
| Claude key | `claude-opus-4-6` | 200，真实后端为 Bedrock Opus 4.6 | 同一公开模型名按 Claude key 路由到 Bedrock |
| Claude key | `claude-sonnet-4-6` | 200，真实后端为 Bedrock Sonnet 4.6 | Claude key 使用公开模型名访问真实 Claude |
| Claude key | `bedrock-claude-opus-4-6` | 401 / 403 `key_model_access_denied` | Bedrock 内部模型名不能被用户直接访问，只能作为 LiteLLM alias 后的内部路由目标 |
| Claude key | `claude-opus-4-7` | 401 / 403 / unsupported | 当前 Bedrock 无 Opus 4.7，不应偷偷降级到 4.6 |
| DeepSeek key | `deepseek-chat` | 200 | DeepSeek key 正常 DeepSeek 通路 |

Claude key 的 Bedrock 回归必须同时覆盖 OpenAI Chat Completions 和 Anthropic Messages 两类入口，并同时覆盖非流式和流式；`claude-*` 公开模型名经 LiteLLM key-specific alias 改写到 `bedrock-claude-*` 后，应由 LiteLLM/Bedrock 路由直接处理，不应再回打 CLIProxyAPI 的 OpenAI 兼容入口:

| 入口 | stream | model | 期望结果 | 目的 |
|---|---:|---|---|---|
| `/v1/chat/completions` | `false` | `claude-opus-4-6` | 200 | 验证 OpenAI Chat Completions 非流式 Bedrock Claude 通路 |
| `/v1/chat/completions` | `true` | `claude-opus-4-6` | 200 | 验证 OpenAI Chat Completions 流式 Bedrock Claude 通路 |
| `/v1/messages?beta=true` | `false` | `claude-opus-4-6` | 200 | 验证 Anthropic Messages 非流式 Bedrock Claude 通路 |
| `/v1/messages?beta=true` | `true` | `claude-opus-4-6` | 200 | 验证 Claude Code 使用的 Anthropic Messages 流式 Bedrock Claude 通路 |

## 上游认证文件

路径: `~/.cli-proxy-api/*.json`

认证文件由人工管理，不要自动复制到新增节点，也不要由 Key Portal 主动触发上游 provider quota API。cliproxyapi 可以热加载认证文件；修改后通常无需重启服务。

## 安全组和网络边界

已记录的安全组:

**node-a (sg-06ef1a045810a0fa9)**

| 端口 | 来源 | 用途 |
|------|------|------|
| 22 | 管理员 IP + 本安全组 | SSH |
| 443 | 0.0.0.0/0 | HTTPS (NLB) |
| 8317 | 172.31.0.0/16 + 本安全组 | 历史/内部 proxy 访问；当前本机链路主要使用 loopback |
| 18080 | 172.31.0.0/16 | Key Portal 回源 |

**工作节点安全组（历史 node-b: sg-0940fb5bea71786a3）**

| 端口 | 来源 | 用途 |
|------|------|------|
| 22 | 管理员 IP + node-a 安全组 | SSH |
| 443 | 0.0.0.0/0 | HTTPS (NLB) |
| 8317 | 172.31.0.0/16 + node-a 安全组 | 历史/内部 proxy 访问；如果 cliproxyapi 只监听 loopback，则不是 NLB 必需路径 |

NLB 只需要能访问各 target 的 TCP 443；`4000`、`8317`、`18081` 应保持本机/内网可见，不应作为公网入口。

## NLB 信息

- 名称: `cliproxy-nlb`
- ARN: `arn:aws:elasticloadbalancing:us-east-2:967519196399:loadbalancer/net/cliproxy-nlb/873bf6ea679ef9dc`
- Target Group ARN: `arn:aws:elasticloadbalancing:us-east-2:967519196399:targetgroup/cliproxy-tls-targets/1cd4d8f8d022ef51`
- 协议: TCP 443
- 健康检查: HTTPS `/healthz`, matcher `200-399`

## DNS

- 域名: `token.zasdas.com`
- DNS 托管: GoDaddy (`ns23` / `ns24.domaincontrol.com`)
- 记录: CNAME → `cliproxy-nlb-873bf6ea679ef9dc.elb.us-east-2.amazonaws.com`

## 文档和配置状态

- `ops/ADD_NODE_RUNBOOK.md`: 新增节点操作手册，应与本文的 Nginx/LiteLLM/health-agent 边界保持一致。
- `key-portal/DEPLOYMENT.md`: 历史单节点迁移文档，旧内容里的 `0.0.0.0:8317`、Key Portal `8080` 和直连 `Client → CLIProxyAPI` 架构不再代表当前生产流量。
- `ops/nginx/cliproxyapi-single-domain.conf`: 仓库中的旧单域名模板，不等同于当前 `/etc/nginx/conf.d/cliproxyapi.conf` 生产配置；应用前必须按当前 LiteLLM 和 health-agent 路由更新。

## 运维注意事项

- node-a 是开发工作区和 Key Portal active 节点；不要无授权覆盖、重启或扰动 node-a 正在运行的服务。
- 新功能/风险变更优先在 node-b 或独立工作节点验证。
- 不要提交 runtime data、数据库、备份、日志、二进制和认证文件。
- 不要在 Key Portal 或监控里主动轮询上游 provider quota API；只读取 proxy/LiteLLM 已经产生的数据。
