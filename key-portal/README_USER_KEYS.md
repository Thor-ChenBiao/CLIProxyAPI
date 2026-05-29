# 用户 Key 管理系统使用指南

## 📋 功能概述

个人 API Key 管理系统，为每个用户分配专属的 API Key，实现：
- ✅ 按用户统计使用量（请求数、Token 数）
- ✅ 一个用户可申请多个 Key（不同设备/项目）
- ✅ 内存缓存，查询速度快
- ✅ 实时统计，自动聚合

---

## 🚀 快速开始

### 第一步：生成 Key 池

首次部署需要预生成一批 Key：

```bash
cd key-portal

# 生成 500 个 Key 并自动添加到 CLIProxyAPI
python generate_keys.py
```

**输出示例：**
```
============================================================
🔑 API Key Pool Generator
============================================================

📝 Generating 500 API keys...
✅ Generated 500 keys

💾 Saving to key-portal/data/key_pool.json...
✅ Saved 500 keys to key-portal/data/key_pool.json

🚀 Adding keys to CLIProxyAPI...
📋 Found 0 existing keys
✅ Added 500 keys to CLIProxyAPI
📊 Total keys in CLIProxyAPI: 500

============================================================
✅ All done! Keys are ready to use.
============================================================
```

### 第二步：启动 Key Portal

```bash
python app.py
```

访问 `http://172.16.70.100:8080`

---

## 👤 用户使用流程

### 1. 申请 API Key

访问：`http://172.16.70.100:8080/register`

填写信息：
- **邮箱**（必填）：用于识别用户
- **姓名**（可选）：显示名称
- **Key 标签**（可选）：如"工作电脑"、"家里电脑"

点击"申请 API Key"，系统返回专属 Key：
```
usr_pool_0001_a1b2c3d4e5f6
```

### 2. 配置 Claude Code

**方式 1：环境变量**
```bash
export ANTHROPIC_API_KEY="usr_pool_0001_a1b2c3d4e5f6"
export ANTHROPIC_BASE_URL="http://172.16.70.100:8317"
```

**方式 2：配置文件**
```json
{
  "apiKey": "usr_pool_0001_a1b2c3d4e5f6",
  "baseUrl": "http://172.16.70.100:8317"
}
```

**方式 3：命令行参数**
```bash
claude-code \
  --api-key usr_pool_0001_a1b2c3d4e5f6 \
  --base-url http://172.16.70.100:8317
```

### 3. 查看使用统计

访问：`http://172.16.70.100:8080/my-keys`

输入邮箱，查看：
- 总请求数、总 Token 数
- 每个 Key 的详细使用情况
- 可以撤销不再使用的 Key

### 4. 申请多个 Key

同一个邮箱可以多次申请 Key，用于：
- 不同设备（工作电脑、家里电脑）
- 不同项目（项目A、项目B）
- 便于统计和管理

---

## 👨‍💼 管理员功能

### 查看所有用户统计

访问：`http://172.16.70.100:8080/admin/users`

功能：
- 📊 总用户数、总 Keys、总请求数、总 Tokens
- 🏆 用户排行榜（按 Token 使用量降序）
- 📈 每个用户的使用占比
- 🔍 点击用户跳转到详情页

### Key 池管理

检查剩余 Key 数量：
```bash
curl http://172.16.70.100:8080/api/key-pool-status
```

响应：
```json
{
  "total": 500,
  "unused": 450,
  "assigned": 50
}
```

如果 Key 不够，重新生成：
```bash
python generate_keys.py
```

---

## 🔍 API 接口文档

### 用户注册
```http
POST /api/register-key
Content-Type: application/json

{
  "email": "user@example.com",
  "name": "张三",
  "label": "工作电脑"
}
```

### 查询用户的 Keys
```http
POST /api/my-keys
Content-Type: application/json

{
  "email": "user@example.com"
}
```

### 撤销 Key
```http
POST /api/revoke-key
Content-Type: application/json

{
  "key": "usr_pool_0001_a1b2c3d4"
}
```

### 查询用户统计
```http
GET /api/user-stats/user@example.com
```

### 查询所有用户统计
```http
GET /api/all-users-stats
```

响应：
```json
{
  "users": [
    {
      "email": "user@example.com",
      "name": "张三",
      "total_requests": 300,
      "total_tokens": 150000,
      "key_count": 2
    }
  ],
  "summary": {
    "total_users": 10,
    "total_requests": 1000,
    "total_tokens": 500000,
    "total_keys": 25
  }
}
```

---

## 📊 数据结构

### key_pool.json
```json
{
  "version": "1.0",
  "generated_at": "2025-01-16T00:00:00Z",
  "total": 500,
  "unused": ["usr_pool_0001_xxx", "usr_pool_0002_xxx"],
  "assigned": {
    "usr_pool_0001_xxx": "user@example.com"
  }
}
```

### user_keys.json
```json
{
  "version": "1.0",
  "users": {
    "user@example.com": {
      "email": "user@example.com",
      "name": "张三",
      "api_keys": ["usr_pool_0001_xxx", "usr_pool_0002_xxx"],
      "created_at": "2025-01-16T00:00:00Z"
    }
  },
  "keys": {
    "usr_pool_0001_xxx": {
      "email": "user@example.com",
      "label": "工作电脑",
      "created_at": "2025-01-16T00:00:00Z"
    }
  }
}
```

---

## 🎯 核心特性

### 1. 内存缓存
- 用户 Key 数据加载到内存，查询速度快
- 统计数据缓存 5 秒，减少 API 调用

### 2. 双向映射
- `users` → 通过邮箱查某人的所有 Key
- `keys` → 通过 Key 查归属人

### 3. 统计聚合
- 自动聚合一个用户所有 Key 的使用量
- 支持按天、按小时、按模型统计

### 4. 热重载
- Key 添加到 CLIProxyAPI 后立即生效
- 无需重启服务

---

## 🔎 LiteLLM Key 归属和用量排查

LiteLLM 不直接用明文 `sk-...` 作为数据库关联键。用户拿到的是明文 key，请求进入 LiteLLM 后会按明文 key 计算 SHA256：

```text
raw key: sk-...
sha256(raw key): <64 hex chars>
```

数据库关联关系：

```text
用户明文 key sk-...
    ↓ sha256
LiteLLM_VerificationToken.token
    ↓ join
LiteLLM_SpendLogs.api_key
    ↓ metadata
metadata.email / metadata.name / metadata.label / metadata.model_group
```

排查某个明文 key 的归属时，先算 SHA256，再用 hash 去查 LiteLLM：

```bash
python3 - <<'PY'
import hashlib
raw_key = 'sk-REPLACE_ME'
print(hashlib.sha256(raw_key.encode()).hexdigest())
PY
```

然后在 LiteLLM Postgres 里查：

```sql
SELECT
  token,
  user_id,
  metadata,
  spend,
  max_budget
FROM "LiteLLM_VerificationToken"
WHERE token = '<sha256(raw key)>';

SELECT
  count(*) AS requests,
  coalesce(sum(total_tokens), 0) AS tokens,
  coalesce(sum(spend), 0) AS spend_usd,
  min("endTime") AS first_seen,
  max("endTime") AS last_seen
FROM "LiteLLM_SpendLogs"
WHERE api_key = '<sha256(raw key)>';
```

模型组费用告警也按这个关联口径统计：只信任 `LiteLLM_VerificationToken.metadata->>'model_group'`，不要用 `SpendLogs.model` 名称推断 key 组，否则 common key 调 Claude 模型会被误算进 Claude key 组。

---

## ❓ 常见问题

### Q1：Key 用完了怎么办？
A：运行 `python generate_keys.py` 生成更多 Key

### Q2：如何统计某个人的用量？
A：访问 `/my-keys` 输入邮箱，或访问 `/admin/users` 查看排行榜

### Q3：用户可以申请多少个 Key？
A：没有限制，只要 Key 池有余量

### Q4：撤销 Key 后会怎样？
A：Key 返回池中可重新分配，CLIProxyAPI 中也会删除

### Q5：统计数据多久更新？
A：实时更新，5秒缓存

### Q6：如何备份数据？
A：定期备份 `key-portal/data/` 目录

---

## 🔧 维护操作

### 查看日志
```bash
tail -f key-portal/portal.log
```

### 手动同步统计
```bash
curl -X POST http://172.16.70.100:8080/api/sync-usage
```

### 清空缓存重新加载
重启 Key Portal：
```bash
pkill -f "python app.py"
python app.py
```

---

## 📈 性能优化

1. **内存缓存**：用户数据和统计数据都缓存在内存
2. **5秒 TTL**：统计数据缓存 5 秒，避免频繁调用 API
3. **批量操作**：Key 池一次性生成 500 个，减少操作次数
4. **索引优化**：双向映射快速查询

---

## 🎉 完成！

现在你的系统支持：
- ✅ 每个用户有专属 API Key
- ✅ 精确统计每个人的使用量
- ✅ 一个邮箱可申请多个 Key
- ✅ 实时排行榜和详细统计

享受使用吧！
