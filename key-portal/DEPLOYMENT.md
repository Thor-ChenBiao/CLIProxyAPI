# CLIProxyAPI + Key Portal 部署与迁移指南

## 系统架构

### 整体架构图

```
┌─────────────────────────────────────────────────────────────────┐
│                         用户端                                    │
│  Claude Code / Cursor / Cline 等 AI 工具                        │
│  配置: ANTHROPIC_BASE_URL + ANTHROPIC_API_KEY                   │
└────────────────────────┬────────────────────────────────────────┘
                         │ HTTP 请求
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│              CLIProxyAPI (Go服务)                                │
│              监听: 0.0.0.0:8317                                  │
│  功能:                                                           │
│  - 请求路由和负载均衡                                             │
│  - Key 池管理 (轮询分配可用 Key)                                  │
│  - 请求统计和用量追踪                                             │
│  - OAuth 认证文件管理 (/data/auth/{email}.json)                 │
└────────────────────────┬────────────────────────────────────────┘
                         │ 使用对应用户的 Key
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│                   Claude API (Anthropic)                         │
│                   api.claude.ai                                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│              Key Portal (Flask Web服务)                          │
│              监听: 0.0.0.0:8080                                  │
│  功能:                                                           │
│  - Web 界面 (使用教程、Key分享、状态监控、用量报表)               │
│  - OAuth 授权流程处理                                            │
│  - Key 状态监控 (通过 CLIProxyAPI 获取)                          │
│  - 飞书通知 (邀请分享、提醒续期)                                  │
│  - 账号管理 (user_mapping.json)                                 │
│  - WebSocket 实时推送 (token 用量)                               │
│  - 定时任务 (过期检查、用量同步)                                  │
└────────────────────────┬────────────────────────────────────────┘
                         │ 读取 Key 状态
                         ▼
┌─────────────────────────────────────────────────────────────────┐
│          CLIProxyAPI Management API                              │
│          http://localhost:8317/v0/management/*                   │
│  - /usage: 用量统计                                              │
│  - /auth-files: 认证文件列表                                     │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                    飞书开放平台                                   │
│  - 发送消息通知                                                  │
│  - 获取用户 open_id                                              │
└─────────────────────────────────────────────────────────────────┘
```

### 核心组件

#### 1. CLIProxyAPI (Go)
- **角色**：核心代理服务
- **端口**：8317
- **数据目录**：`/data/auth/` (存储 OAuth 认证文件)
- **配置文件**：`config.yaml`
- **功能**：
  - 请求代理转发
  - Key 池管理和轮询分配
  - 用量统计 API
  - 认证文件管理

#### 2. Key Portal (Python Flask)
- **角色**：管理 Web 界面
- **端口**：8080
- **主要文件**：
  - `app.py` - Flask 应用主程序
  - `config.py` - 配置文件 (飞书配置、服务地址)
  - `user_mapping.json` - 账号映射表
  - `templates/index.html` - Web 界面
- **依赖**：
  - flask
  - flask-socketio
  - requests
  - apscheduler
- **功能**：
  - OAuth 授权流程
  - Key 状态可视化
  - 飞书通知
  - 实时用量推送 (WebSocket)
  - 定时任务调度

### 数据流向

1. **OAuth 授权流程**：
   ```
   Portal → Claude OAuth → Portal → CLIProxyAPI/data/auth/
   ```

2. **API 请求流程**：
   ```
   Client → CLIProxyAPI → Claude API
   ```

3. **状态查询流程**：
   ```
   Portal → CLIProxyAPI Management API
   ```

4. **通知推送流程**：
   ```
   Portal → 飞书 API → 用户
   ```

---

## 环境迁移指南

### 场景说明

当需要将服务从一个环境迁移到另一个环境，或者更换服务器IP地址时，需要修改多处配置。

**示例**：从 `172.16.70.100` 迁移到 `192.168.1.100`

### 配置修改清单

#### 1. Key Portal 配置文件

**文件**：`key-portal/config.py`

**修改内容**：
```python
SERVICE_INFO = {
    "base_url": "http://192.168.1.100:8317",  # 改为新IP
    ...
}
```

**命令**：
```bash
sed -i 's|http://172.16.70.100:8317|http://192.168.1.100:8317|g' /root/CLIProxyAPI/key-portal/config.py
```

---

#### 2. Key Portal 前端页面

**文件**：`key-portal/templates/index.html`

**修改位置**：
- Claude Code 配置命令中的 `ANTHROPIC_BASE_URL`
- API 地址展示
- 其他硬编码的服务地址

**命令**：
```bash
sed -i 's|172.16.70.100|192.168.1.100|g' /root/CLIProxyAPI/key-portal/templates/index.html
```

---

#### 3. Key Portal 飞书通知

**文件**：`key-portal/app.py`

**修改位置**：
- 邀请分享通知中的链接
- 提醒续期通知中的链接
- 过期提醒中的链接

**命令**：
```bash
sed -i 's|http://172.16.70.100:8080|http://192.168.1.100:8080|g' /root/CLIProxyAPI/key-portal/app.py
```

---

#### 4. CLIProxyAPI 配置 (可选)

**文件**：`config.yaml`

默认配置已设置为监听所有接口 (`host: ""`)，通常不需要修改。

如需指定监听IP：
```yaml
host: "192.168.1.100"  # 或保持为 "" 监听所有接口
port: 8317
```

---

### 自动化迁移脚本

创建 `migrate_ip.sh`：

```bash
#!/bin/bash
# IP地址迁移脚本

OLD_IP="172.16.70.100"
NEW_IP="$1"

if [ -z "$NEW_IP" ]; then
    echo "用法: $0 <新IP地址>"
    echo "示例: $0 192.168.1.100"
    exit 1
fi

echo "=========================================="
echo "IP 迁移工具"
echo "=========================================="
echo "当前IP: $OLD_IP"
echo "目标IP: $NEW_IP"
echo ""

# 进入项目目录
PORTAL_DIR="/root/CLIProxyAPI/key-portal"
cd "$PORTAL_DIR" || { echo "错误: 找不到目录 $PORTAL_DIR"; exit 1; }

# 备份原始文件
echo "[1/4] 备份配置文件..."
cp config.py config.py.bak.$(date +%Y%m%d_%H%M%S)
cp templates/index.html templates/index.html.bak.$(date +%Y%m%d_%H%M%S)
cp app.py app.py.bak.$(date +%Y%m%d_%H%M%S)
echo "✓ 备份完成"

# 替换配置文件
echo ""
echo "[2/4] 替换 config.py..."
sed -i "s|$OLD_IP|$NEW_IP|g" config.py
grep -q "$NEW_IP" config.py && echo "✓ config.py 更新成功" || echo "✗ config.py 更新失败"

echo ""
echo "[3/4] 替换 templates/index.html..."
sed -i "s|$OLD_IP|$NEW_IP|g" templates/index.html
grep -q "$NEW_IP" templates/index.html && echo "✓ index.html 更新成功" || echo "✗ index.html 更新失败"

echo ""
echo "[4/4] 替换 app.py..."
sed -i "s|$OLD_IP|$NEW_IP|g" app.py
grep -q "$NEW_IP" app.py && echo "✓ app.py 更新成功" || echo "✗ app.py 更新失败"

# 显示修改结果
echo ""
echo "=========================================="
echo "修改验证"
echo "=========================================="
echo ""
echo "config.py:"
grep "$NEW_IP" config.py | head -2
echo ""
echo "index.html:"
grep "$NEW_IP" templates/index.html | head -2
echo ""
echo "app.py:"
grep "$NEW_IP" app.py | head -2

echo ""
echo "=========================================="
echo "✓ IP 迁移完成！"
echo "=========================================="
echo ""
echo "接下来请执行："
echo "1. 重启 Key Portal 服务："
echo "   pkill -9 -f 'python.*app.py'"
echo "   cd $PORTAL_DIR && python3 app.py > portal.log 2>&1 &"
echo ""
echo "2. 验证服务："
echo "   curl http://$NEW_IP:8080"
echo "   curl http://$NEW_IP:8317/v1/models"
echo ""
echo "3. 通知用户更新配置命令"
```

### 使用迁移脚本

```bash
# 1. 赋予执行权限
chmod +x migrate_ip.sh

# 2. 执行迁移
./migrate_ip.sh 192.168.1.100

# 3. 重启服务
pkill -9 -f 'python.*app.py'
cd /root/CLIProxyAPI/key-portal && python3 app.py > portal.log 2>&1 &

# 4. 验证服务
curl http://192.168.1.100:8080
curl http://192.168.1.100:8317/v1/models
```

---

## 初始部署指南

### 前置要求

- Python 3.8+
- Go 1.19+ (用于编译 CLIProxyAPI)
- 飞书开放平台应用 (App ID 和 App Secret)

### 部署步骤

#### 1. 部署 CLIProxyAPI

```bash
# 克隆仓库
git clone https://github.com/your-org/CLIProxyAPI.git
cd CLIProxyAPI

# 编译
go build -o cliproxy cmd/server/main.go

# 创建配置文件
cat > config.yaml << 'EOF'
host: ""
port: 8317
remote-management:
  allow-remote: true
  secret-key: "$2a$10$2rCWYIJSFURMsZ1nb5qy7uHeggUTU9x/tnVXGJXj9n3Cii1otezKq"
  disable-control-panel: false
auth-dir: "/root/.cli-proxy-api"
api-keys:
  - "sk-shared-001"
  - "any-key"
oauth-callbacks:
  claude: "http://localhost:54545/callback"
debug: false
usage-statistics-enabled: true
EOF

# 启动服务
./cliproxy &
```

---

#### 2. 部署 Key Portal

```bash
cd key-portal

# 安装依赖
pip3 install flask flask-socketio requests apscheduler

# 创建配置文件
cat > config.py << 'EOF'
# CLIProxyAPI Management API
CLIPROXY_API_URL = "http://localhost:8317"
CLIPROXY_MANAGEMENT_KEY = "admin123"

# Feishu App credentials
FEISHU_APP_ID = "cli_xxxxxxxxxxxxxxxx"
FEISHU_APP_SECRET = "xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"

# Key expiry settings
KEY_EXPIRE_WARNING_HOURS = 2
KEY_CHECK_INTERVAL_MINUTES = 30

# Server settings
HOST = "0.0.0.0"
PORT = 8080

# Service info (替换为实际IP)
SERVICE_INFO = {
    "base_url": "http://YOUR_SERVER_IP:8317",
    "available_models": [
        "claude-sonnet-4-5-20250929",
        "claude-opus-4-5-20251101",
    ],
}
EOF

# 创建账号映射文件
cat > user_mapping.json << 'EOF'
{
    "users": [
        {
            "claude_email": "user1-claude@company.com",
            "feishu_email": "user1@company.com",
            "name": "User One"
        }
    ]
}
EOF

# 启动服务
python3 app.py > portal.log 2>&1 &
```

---

#### 3. 配置用户端

用户需要执行以下配置命令（替换为实际IP）：

```bash
echo '{"apiKeyHelper":"echo sk-shared-001","env":{"ANTHROPIC_BASE_URL":"http://YOUR_SERVER_IP:8317"}}' > ~/.claude/settings.json && echo '{"hasCompletedOnboarding":true,"customApiKeyResponses":{"approved":["001"],"rejected":[]}}' > ~/.claude.json && cat ~/.claude/settings.json && cat ~/.claude.json
```

配置完成后，重启终端，直接运行 `claude` 命令即可使用。

---

## 验证检查清单

迁移或部署完成后，请依次验证以下项目：

- [ ] **CLIProxyAPI 服务运行**
  ```bash
  curl http://YOUR_IP:8317/v1/models
  ```

- [ ] **Key Portal 可访问**
  ```bash
  curl http://YOUR_IP:8080
  ```

- [ ] **教程页面显示正确的IP地址**
  - 访问 http://YOUR_IP:8080
  - 检查 "Claude Code 配置" 中的 ANTHROPIC_BASE_URL

- [ ] **飞书通知链接正确**
  - 通过 Key Portal 发送测试通知
  - 检查飞书消息中的链接是否指向新IP

- [ ] **OAuth 授权流程正常**
  - 点击 "分享 Key" → "打开 Claude 授权"
  - 完成授权流程

- [ ] **Key 状态页面显示正常**
  - 查看已激活的 Key 列表
  - 查看账号列表和状态

- [ ] **用户端配置正常**
  - 执行配置命令
  - 运行 `claude` 测试 API 请求

---

## 常见问题

### 1. 服务无法启动

**症状**：`python3 app.py` 启动后立即退出

**解决**：
```bash
# 查看错误日志
cat portal.log

# 检查依赖
pip3 install flask flask-socketio requests apscheduler

# 检查端口占用
lsof -i:8080
```

---

### 2. 飞书通知发送失败

**症状**：点击发送通知后提示 "发送失败"

**可能原因**：
- 飞书 App ID 或 App Secret 配置错误
- 用户在飞书中的邮箱与 user_mapping.json 中不匹配
- 网络无法访问飞书 API

**解决**：
```bash
# 检查配置
cat config.py | grep FEISHU

# 查看日志
tail -f portal.log
```

---

### 3. Key 状态页面显示 "加载中..."

**症状**：Key 状态页面一直显示加载中，无数据

**可能原因**：
- CLIProxyAPI 服务未启动
- config.py 中的 CLIPROXY_API_URL 配置错误
- 认证文件目录为空

**解决**：
```bash
# 检查 CLIProxyAPI 状态
curl http://localhost:8317/v0/management/auth-files

# 检查认证文件
ls -la /root/.cli-proxy-api/
```

---

### 4. 用户配置后仍需要登录

**症状**：用户执行配置命令后，运行 `claude` 仍提示登录

**解决**：
```bash
# 1. 退出已有登录
claude /logout

# 2. 重新配置
echo '{"apiKeyHelper":"echo sk-shared-001","env":{"ANTHROPIC_BASE_URL":"http://YOUR_IP:8317"}}' > ~/.claude/settings.json && echo '{"hasCompletedOnboarding":true,"customApiKeyResponses":{"approved":["001"],"rejected":[]}}' > ~/.claude.json

# 3. 完全重启终端（重要！）
# 4. 再次运行 claude 命令
```

---

## 维护建议

### 日志管理

```bash
# 定期清理日志
find /root/CLIProxyAPI/key-portal -name "*.log" -mtime +7 -delete

# 查看实时日志
tail -f /root/CLIProxyAPI/key-portal/portal.log
```

### 备份

```bash
# 备份配置文件
tar -czf key-portal-backup-$(date +%Y%m%d).tar.gz \
    /root/CLIProxyAPI/key-portal/config.py \
    /root/CLIProxyAPI/key-portal/user_mapping.json \
    /root/CLIProxyAPI/config.yaml

# 备份认证文件
tar -czf auth-backup-$(date +%Y%m%d).tar.gz /root/.cli-proxy-api/
```

### 监控

建议监控以下指标：
- CLIProxyAPI 服务状态 (端口 8317)
- Key Portal 服务状态 (端口 8080)
- 认证文件数量和过期状态
- 飞书通知发送成功率

---

## 技术支持

如有问题，请检查：
1. 服务日志：`/root/CLIProxyAPI/key-portal/portal.log`
2. CLIProxyAPI 日志
3. 网络连通性：`ping`、`telnet`

---

**文档版本**：v1.0
**最后更新**：2026-01-16
