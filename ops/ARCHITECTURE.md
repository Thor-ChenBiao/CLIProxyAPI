# CLIProxyAPI 集群架构

最后更新: 2026-05-23

## 节点信息

| 节点 | 实例ID | 实例类型 | IP (EIP) | 内网IP | 用途 |
|------|--------|---------|----------|--------|------|
| node-a (biao-go-server) | i-0afbeaa90d8dc91e1 | c7g.xlarge (4vCPU/8GB) | 3.149.220.73 | 172.31.17.144 | Nginx + LiteLLM + cliproxyapi + key-portal |
| node-b (cliproxy-node-b) | i-08055c52390086849 | c7g.large (2vCPU/4GB) | 3.150.54.188 | 172.31.26.28 | Nginx + LiteLLM + cliproxyapi |
| node-c (cliproxy-node-c) | i-07d065661a87679df | c7g.large (2vCPU/4GB) | 18.189.167.58 | 172.31.16.7 | Nginx + LiteLLM + cliproxyapi |
| node-d (cliproxy-node-d) | i-0fd32ef2df7d69857 | c7g.large (2vCPU/4GB) | 3.19.6.233 | 172.31.24.86 | Nginx + LiteLLM + cliproxyapi |

区域: us-east-2, VPC: vpc-0f364ac1dc5cb2e11, 子网: subnet-08a5e4e391a7304c4 (us-east-2b)

## 流量架构

```
用户请求 → token.zasdas.com (CNAME)
    │
    ▼
┌──────────────────────────────────────┐
│  NLB: cliproxy-nlb                   │
│  DNS: cliproxy-nlb-873bf6ea679ef9dc  │
│       .elb.us-east-2.amazonaws.com   │
│  Target Group: cliproxy-tls-targets  │
│  协议: TCP:443 透传                   │
│  健康检查: HTTPS /healthz            │
└──────┬───────────────┬───────────────┘
       │               │
       ▼               ▼
   node-a Nginx    node-b Nginx
   :443 (SSL)      :443 (SSL)
       │               │
       ▼               ▼
   LiteLLM         LiteLLM
   :4000          :4000
       │               │
       ▼               ▼
   cliproxyapi     cliproxyapi
   127.0.0.1:8317  172.31.26.28:8317
```

当前 `/v1/*` 模型请求链路为: Nginx → LiteLLM → cliproxyapi → 上游模型服务。
Nginx 保留来源 IP / 网络层访问控制和旧 key 兼容 rewrite；LiteLLM 统一负责用户 key 校验、模型权限、预算和限流；cliproxyapi 不再维护用户 API key allowlist，只负责模型代理和上游适配。

注意: DNS 切换到 NLB 后, node-a 的 upstream 应改为只 proxy 本机 (去掉对 node-b 内网转发)。

## 服务说明

| 服务 | 端口 | 部署节点 | 说明 |
|------|------|---------|------|
| Nginx | 443 | node-a/b/c/d | SSL 终端, 反向代理, 来源 IP / 网络层访问控制 |
| LiteLLM | 4000 | node-a/b/c/d | 用户 API key 校验、模型权限、预算、限流、审计入口 |
| cliproxyapi | 8317 | node-a/b/c/d | API 代理核心服务 (Go), 不再校验用户 API key |
| key-portal | 18080 | 仅 node-a | 用户密钥管理/用量面板 (Python) |

key-portal 只在 node-a 运行。node-b/node-c/node-d 的 Nginx 对 `/`、`/admin/*`、`/socket.io/*` 和 portal API 路径 proxy 回 node-a 的 key-portal (172.31.17.144:18080)。

## 认证与授权边界

- Nginx: 保留来源 IP / 网络层访问控制, 管理路径 allowlist, 以及 `usr_pool_*` 到 `sk-usr_pool_*` 的兼容 rewrite。
- LiteLLM: `/v1/*` 用户 API key 的唯一校验点, 负责 key 是否存在、是否 blocked、模型权限、预算、限流和审计。
- cliproxyapi: 两节点 `api-keys` 已清空, 不再注册 config API-key provider, 不再校验用户 API key。直接访问 cliproxyapi 的 `/v1/models` 无 key 也会返回 200；外部正式入口仍由 Nginx/LiteLLM 拦截 no-key 和 bad-key。

## 模型路由规则

用户侧使用统一的公开模型名，特别是 Claude 兼容名应保持为 `claude-*`，例如 `claude-opus-4-7`。不要要求用户为了选择真实 Bedrock Claude 而改用 `bedrock-claude-*` 或 `bedrock/...` 这类内部路由名；真实后端必须由 API key 的模型组决定。

关键规则:

- common / GPT key 请求 `claude-*` 公开模型名时，应走 GPT-backed Claude 兼容路由，不走 Bedrock。
- Claude key 请求同一个 `claude-*` 公开模型名时，应走真实 Claude / Bedrock 路由，并消耗 Claude key 的总额度。
- DeepSeek key 请求 `deepseek-*` 公开模型名时，应走 DeepSeek provider，并消耗 DeepSeek key 的总额度。
- `bedrock-claude-*`、`bedrock/...` 等名称是内部实现细节，不应作为用户侧模型选择规则，也不应成为区分 common key 和 Claude key 的用户操作方式。

实现方式:

- common key 的 LiteLLM allowlist 包含 GPT 模型和 GPT-backed 的 `claude-*` 公开模型名，不设置 key-specific aliases；请求到达 cliproxyapi 后由 cliproxyapi 的 `oauth-model-alias` 路由到 GPT/Codex provider。
- Claude key 的 LiteLLM allowlist 只暴露 `claude-*` 公开模型名，同时在 key 记录上设置 key-specific `aliases`，例如 `claude-opus-4-6` → `bedrock-claude-opus-4-6`；LiteLLM 在转发前按 key 改写到内部 Bedrock 模型名。
- key-portal 只在 node-a active，但 `key-portal/app.py` 必须同步到 node-b，避免未来切换 key-portal 时生成不同策略的 key。LiteLLM key 记录存储在共享数据库中，现有 key 的 allowlist / aliases 迁移只需对 LiteLLM 数据库执行一次，两节点都会生效。

回归测试用例必须从正式入口 `https://token.zasdas.com/v1/chat/completions` 发起，模拟用户行为，而不是只打本机端口:

| Key 类型 | 用户请求 model | 期望结果 | 目的 |
|---|---|---|---|
| common / GPT key | `gpt-5.5` | 200 | common key 正常 GPT 通路 |
| common / GPT key | `claude-opus-4-6` | 200，返回公开模型名仍为 `claude-opus-4-6` | common key 使用 GPT-backed Claude 兼容路由 |
| common / GPT key | `bedrock-claude-opus-4-6` | 401 `key_model_access_denied` | common key 不能访问内部 Bedrock 模型名 |
| common / GPT key | `deepseek-chat` | 401 `key_model_access_denied` | common key 不能访问 DeepSeek |
| Claude key | `claude-opus-4-6` | 200，真实后端为 Bedrock Opus 4.6 | 同一公开模型名按 Claude key 路由到 Bedrock |
| Claude key | `claude-sonnet-4-6` | 200，真实后端为 Bedrock Sonnet 4.6 | Claude key 使用公开模型名访问真实 Claude |
| Claude key | `claude-opus-4-7` | 401 / unsupported | 当前 Bedrock 无 Opus 4.7，不应偷偷降级到 4.6 |
| DeepSeek key | `deepseek-chat` | 200 | DeepSeek key 正常 DeepSeek 通路 |

测试时禁止在用户输出中打印完整 key；只展示 key 类型、模型名、HTTP 状态和必要错误类型。

## 上游认证文件

路径: `~/.cli-proxy-api/*.json`

两节点各 10 个认证文件, 均衡分布。cliproxyapi 热加载, 修改后无需重启。

## 安全组

**node-a (sg-06ef1a045810a0fa9)**

| 端口 | 来源 | 用途 |
|------|------|------|
| 22 | 管理员 IP + 本安全组 | SSH |
| 443 | 0.0.0.0/0 | HTTPS (NLB) |
| 8317 | 172.31.0.0/16 + 本安全组 | 集群内部 |
| 18080 | 172.31.0.0/16 | key-portal (node-b 回访) |

**node-b (sg-0940fb5bea71786a3)**

| 端口 | 来源 | 用途 |
|------|------|------|
| 22 | 管理员 IP + node-a 安全组 | SSH |
| 443 | 0.0.0.0/0 | HTTPS (NLB) |
| 8317 | 172.31.0.0/16 + node-a 安全组 | 集群内部 |

## NLB 信息

- 名称: cliproxy-nlb
- ARN: arn:aws:elasticloadbalancing:us-east-2:967519196399:loadbalancer/net/cliproxy-nlb/873bf6ea679ef9dc
- Target Group ARN: arn:aws:elasticloadbalancing:us-east-2:967519196399:targetgroup/cliproxy-tls-targets/1cd4d8f8d022ef51
- 健康检查: HTTPS, /healthz, 10s 间隔, 2次阈值

## DNS

- 域名: token.zasdas.com
- DNS 托管: GoDaddy (ns23/ns24.domaincontrol.com)
- 记录: CNAME → cliproxy-nlb-873bf6ea679ef9dc.elb.us-east-2.amazonaws.com

## 待办

- [ ] DNS CNAME 切换完成后, 修改 node-a upstream 为只 proxy 本机
- [ ] 确认 EIP 3.149.220.73 是否仍需保留 (SSH 管理用)
