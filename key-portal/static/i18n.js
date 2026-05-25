(function () {
    "use strict";
    if (window.__keyPortalI18nLoaded) return;
    window.__keyPortalI18nLoaded = true;

    const STORAGE_KEY = "keyPortalLang";
    const LANG_ZH = "zh";
    const LANG_EN = "en";
    const HAN_RE = /[\u3400-\u9fff]/;
    const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA"]);
    const SKIP_CLOSEST = "pre, code";
    const ATTRS = ["placeholder", "title", "aria-label"];
    const SHELL_TOGGLE_SELECTOR = ".portal-lang-toggle";

    const EXACT = {
        "AI 能量站": "AI Access Hub",
        "Token 是 AI 时代的电力，用起来才能创造价值！": "Tokens are the fuel for AI work. Put them to use.",
        "Token 是 AI 时代的": "Tokens are the fuel for AI work",
        "电力": "",
        "，用起来才能创造价值！": ". Put them to use.",
        "今日 Tokens": "Tokens today",
        "今日请求数": "Requests today",
        "累计 Tokens": "Total tokens",
        "累计请求数": "Total requests",
        "申请个人 Key": "Get a personal key",
        "用量报表": "Usage",
        "申请个人 API Key": "Get your personal API key",
        "使用教程": "Setup guide",
        "查看统计": "View usage",
        "剩余可用 Key": "Keys available",
        "标识 *": "Label *",
        "邮箱 或 描述（如：张三-工作电脑、AI项目测试、产品部）": "Email or a short label, such as Alex - work laptop, AI project test, Product team",
        "用于识别和统计，可以是邮箱、姓名、场景描述等": "Used for usage tracking. Use an email, name, team, or project label.",
        "申请 API Key": "Get API key",
        "申请中...": "Creating key...",
        "公司级 Claude Skills": "Company Claude Skills",
        "提升 Claude Code 能力，安装公司定制的技能包": "Install the team's Claude Code skills package.",
        "访问 GitHub 仓库 →": "Open GitHub repo →",
        "访问 GitHub 仓库": "Open GitHub repo",
        "我申请过的 Keys": "My keys",
        "用户用量": "User usage",
        "刷新数据": "Refresh",
        "累计 Token": "Total tokens",
        "总请求数": "Requests",
        "成功率": "Success rate",
        "用量趋势": "Usage trend",
        "详细数据": "Details",
        "日期": "Date",
        "Token 消耗": "Tokens",
        "请求数": "Requests",
        "加载中...": "Loading...",
        "暂无数据": "No data yet",
        "加载失败": "Could not load",
        "时": "Hour",
        "日": "Day",
        "月": "Month",
        "年": "Year",
        "复制": "Copy",
        "已复制": "Copied",
        "已复制!": "Copied",
        "复制失败，请手动复制": "Copy failed. Please copy it manually.",
        "已复制到剪贴板！": "Copied to clipboard.",
        "已复制到剪贴板": "Copied to clipboard.",
        "请输入标识": "Enter a label.",
        "申请成功！": "Your key is ready",
        "API Key：": "API key:",
        "API URL：": "API URL:",
        "Claude Code 一键配置命令：": "Claude Code setup command:",
        "复制命令到终端执行，即可自动配置 Claude Code": "Copy and run this in your terminal to configure Claude Code.",
        "Codex CLI 一键配置命令：": "Codex CLI setup command:",
        "同一个 API Key 也可用于 Codex CLI，复制到终端执行即可": "Use the same API key for Codex CLI. Copy and run this in your terminal.",
        "下一步：安装公司级 Claude Skills": "Next: install the company Claude Skills",
        "申请失败": "Could not create the key",
        "网络错误": "Network error",
        "我的 Keys - Key Portal": "My Keys - Key Portal",
        "我的 API Keys": "My API keys",
        "返回首页": "Home",
        "输入你的 API Key（如：usr_pool_0001_xxxx）": "Enter your API key, for example usr_pool_0001_xxxx",
        "查询": "Search",
        "如何查看我的 API Key？": "How do I find my API key?",
        "在终端执行以下命令：": "Run this in your terminal:",
        "输出中 \"echo\" 后面的就是你的 API Key": "Your API key is the value after \"echo\".",
        "安全移除代理配置": "Remove proxy settings safely",
        "如需恢复为官方 Claude Code（移除代理配置但保留 MCP/permissions 等设置）：": "To go back to the official Claude Code connection, remove only the proxy settings while keeping MCP, permissions, and other settings:",
        "如需恢复为官方 Claude Code（仅移除代理相关配置，保留 MCP、permissions 等设置）：": "To go back to the official Claude Code connection, remove only the proxy settings while keeping MCP, permissions, and other settings:",
        "复制移除命令": "Copy removal command",
        "复制后在终端粘贴执行，仅移除 apiKeyHelper 和 ANTHROPIC_BASE_URL，不影响其他配置": "Copy it, paste it in your terminal, and run it. It only removes apiKeyHelper and ANTHROPIC_BASE_URL.",
        "总 Tokens": "Total tokens",
        "Keys 数量": "Keys",
        "您的 API Keys": "Your API keys",
        "标签": "Label",
        "创建时间": "Created",
        "操作": "Actions",
        "Claude 配置": "Claude setup",
        "Codex 配置": "Codex setup",
        "查看详情": "Details",
        "删除": "Remove",
        "请输入 API Key": "Enter an API key.",
        "确定要撤销这个 Key 吗？此操作不可恢复！": "Revoke this key? This cannot be undone.",
        "Key 已撤销": "Key revoked.",
        "撤销": "Revoke",
        "贡献 Key - CLIProxyAPI": "Contribute a Key - CLIProxyAPI",
        "贡献 Key": "Contribute a key",
        "通过 OAuth 授权贡献你的 Claude 账号，让团队共享使用": "Connect a Claude account through OAuth so the team can use the shared pool.",
        "开始 Claude 授权": "Start Claude authorization",
        "点击下方按钮，系统会打开 Claude 登录页面。请使用你的 Claude 账号登录并完成授权。": "Click the button below. A Claude login window will open. Sign in with your Claude account and approve access.",
        "开始授权": "Start authorization",
        "正在获取授权链接...": "Getting authorization link...",
        "已打开授权页面": "Authorization page opened",
        "复制回调地址": "Copy the callback URL",
        "重要提示：": "Important:",
        "这是正常的！": "This is expected.",
        "授权完成后，新窗口会显示\"无法访问此网站\"或类似错误页面。": "After authorization, the new window may show \"This site can't be reached\" or a similar error.",
        "请复制浏览器地址栏中的完整地址。": " Copy the full URL from the browser address bar.",
        "地址栏中的 URL 类似这样：": "The URL in the address bar will look like this:",
        "从剪贴板粘贴": "Paste from clipboard",
        "在这里粘贴完整的回调地址...": "Paste the full callback URL here...",
        "提交完成授权": "Finish authorization",
        "正在验证授权信息，请稍候...": "Verifying authorization. Please wait...",
        "返回教程": "Back to guide",
        "查看 Key 状态 →": "View key status →",
        "请粘贴回调地址": "Paste the callback URL.",
        "地址格式不正确，请确保复制完整的地址（需要包含 code= 和 state= 参数）": "The URL format is not valid. Make sure it includes both code= and state=.",
        "授权成功！": "Authorization complete",
        "已添加": "Added",
        "Key 有效期约 8 小时，过期后请重新授权。": "The key is valid for about 8 hours. Authorize again after it expires.",
        "你的 Claude Key 已成功注册，现在可以通过 API 使用了。": "Your Claude key has been registered and is ready for API use.",
        "申请 API Key - Key Portal": "Get API Key - Key Portal",
        "填写信息获取您的专属 API Key": "Enter your information to get a personal API key.",
        "剩余可用 Key：": "Keys available:",
        "邮箱 *": "Email *",
        "姓名（可选）": "Name (optional)",
        "Key 标签（可选）": "Key label (optional)",
        "工作电脑": "Work laptop",
        "查看我的 Keys": "View my keys",
        "用户统计": "User stats",
        "您的 API Key：": "Your API key:",
        "请妥善保管此 Key，配置到 Claude Code 中使用。": "Keep this key safe and use it to configure Claude Code.",
        "Key 状态 - CLIProxyAPI": "Key Status - CLIProxyAPI",
        "Key 状态监控": "Key status",
        "查看所有已注册的 Claude Key 状态": "Monitor all registered Claude keys.",
        "概览": "Overview",
        "刷新": "Refresh",
        "总 Key 数": "Total keys",
        "有效": "Active",
        "即将过期": "Expiring soon",
        "已过期": "Expired",
        "Key 列表": "Key list",
        "暂无注册的 Key": "No registered keys yet",
        "过期时间:": "Expires:",
        "用户统计 - Key Portal": "User Stats - Key Portal",
        "用户使用统计": "User usage",
        "实时更新，按 Token 使用量排序": "Live usage, sorted by token usage.",
        "总用户数": "Users",
        "总 Keys": "Keys",
        "累计总量": "All time",
        "按日": "Daily",
        "按月": "Monthly",
        "按年": "Yearly",
        "导出 Excel": "Export CSV",
        "正在加载...": "Loading...",
        "排名": "Rank",
        "用户": "User",
        "邮箱": "Email",
        "Token 使用": "Token usage",
        "占比": "Share",
        "时间": "Period",
        "Claude Code 使用教程": "Claude Code setup guide",
        "执行配置命令": "Run the setup command",
        "申请成功后，复制显示的": "After your key is created, copy the ",
        "\"Claude Code 一键配置命令\"": "\"Claude Code setup command\"",
        "，粘贴到终端执行：": " and run it in your terminal:",
        "说明：": "Note:",
        "💡 说明：": "Tip:",
        "这条命令会": "This command ",
        "合并更新": "merges updates into",
        "，只修改 API Key 和服务器地址，保留你已有的 MCP、permissions 等配置": ". It only changes the API key and server URL, and keeps your MCP, permissions, and other settings.",
        "验证配置": "Verify the setup",
        "运行": "Run",
        "命令，Claude Code 会自动读取配置的 API Key 和 Base URL": "command. Claude Code will read the API key and base URL from your settings.",
        "开始使用": "Start using it",
        "现在可以正常使用 Claude Code 了！以下是一些常用命令示例：": "You can now use Claude Code normally. Here are a few examples:",
        "基础对话：": "Basic prompt:",
        "代码项目：": "Project work:",
        "交互模式：": "Interactive mode:",
        "重要提示": "Tips",
        "配置文件保存在": "Your settings are saved in",
        "，重启终端后依然有效": " and will keep working after you restart the terminal.",
        "如需更换 API Key，只需重新执行配置命令即可覆盖": "To switch API keys, just run the setup command again.",
        "多台电脑使用：每台电脑都需要执行一次配置命令": "Using multiple computers? Run the setup once on each machine.",
        "手动配置方式（可选）": "Manual setup (optional)",
        "如果需要手动配置，可以创建": "If you prefer to configure it manually, create",
        "文件：": " file:",
        "Codex CLI 配置": "Codex CLI setup",
        "同一个 API Key 也可用于 OpenAI Codex CLI，执行以下命令一键配置：": "The same API key also works with OpenAI Codex CLI. Run this command to configure it:",
        "复制 Codex 配置命令": "Copy Codex setup command",
        "API Key 与 Claude Code 通用，用你申请到的同一个 Key 即可。设置完后运行": "Use the same API key you created for Claude Code. After setup, run",
        "即可使用。": "to start using it.",
        "帮我写一个Python快速排序函数": "Write a Python quicksort function",
        "分析这个项目的代码结构": "Analyze this project's code structure",
        "直接进入交互式对话": "Start an interactive session",
        "看到这个提示说明配置成功": "This means the setup loaded correctly",
        "总览": "Overview",
        "服务状态": "Service status",
        "认证统计": "Auth stats",
        "LiteLLM 后台": "LiteLLM admin",
        "主导航": "Main navigation",
        "已登录": "Signed in",
        "退出": "Log out",
        "语言切换": "Language switcher",
        "认证文件统计 - Key Portal": "Auth File Stats - Key Portal",
        "认证文件整体统计": "Auth file health",
        "认证明细优先展示 5 小时和 7 天两个关键窗口；认证文件和流量实时读取节点管理接口，Key Portal 不主动查询 provider 原生额度。": "Auth details focus on the key 5-hour and 7-day windows. Auth files and traffic are read live from node management APIs; Key Portal does not directly poll provider quota APIs.",
        "屏蔽告警": "Mute alerts",
        "恢复告警": "Resume alerts",
        "飞书告警正常发送": "Feishu alerts are active",
        "飞书告警已屏蔽，不影响健康检查、事件记录和恢复处理": "Feishu alerts are muted. Health checks, event logging, and recovery still run.",
        "节点健康视图": "Node health view",
        "按节点": "By node",
        "按认证文件": "By auth file",
        "刷新认证统计": "Refresh auth stats",
        "节点": "Node",
        "认证文件": "Auth files",
        "可用": "Active",
        "异常": "Warning",
        "不可用": "Unavailable",
        "需关注": "Needs attention",
        "需处理": "Needs action",
        "健康": "Healthy",
        "无认证文件": "No auth files",
        "有不可用": "Has unavailable files",
        "点击展开 / 收起认证文件明细": "Click to expand or collapse auth file details",
        "管理后台": "Management",
        "按节点看流量": "Traffic by node",
        "按认证文件看流量": "Traffic by auth file",
        "额度剩余": "Quota remaining",
        "健康状态": "Health status",
        "请求负载": "Request load",
        "次级指标": "Secondary metrics",
        "最近请求": "Latest request",
        "最近失败": "Latest failure",
        "请求": "Requests",
        "成功 / 失败": "Success / failure",
        "失败率": "Failure rate",
        "均值 Tokens/请求": "Avg tokens/request",
        "费用": "Cost",
        "分类": "Breakdown",
        "限额": "Limit",
        "剩余": "Remaining",
        "原生窗口": "Native window",
        "暂无窗口": "No window",
        "未获取": "Not fetched",
        "暂未接入": "Not supported yet",
        "获取失败": "Fetch failed",
        "最近 24 小时": "Last 24 hours",
        "最近 7 天": "Last 7 days",
        "最近 30 天": "Last 30 days",
        "成功": "Success",
        "失败": "Failure",
        "禁用": "Disabled",
        "最近异常": "Recent errors",
        "认证文件已禁用，不参与调度。": "This auth file is disabled and is not used for routing.",
        "认证文件被标记为不可用，需要人工处理。": "This auth file is marked unavailable and needs manual action.",
        "最近有失败记录。": "Recent failures were found.",
        "最近 5 小时有失败请求，但账号未被标记为不可用。": "There were failures in the last 5 hours, but the account is not marked unavailable.",
        "最近 5 小时有成功请求。": "There were successful requests in the last 5 hours.",
        "当前未发现不可用标记。": "No unavailable flag detected.",
        "停用认证": "Disable auth",
        "启用认证": "Enable auth",
        "今日预估额度消耗": "Estimated quota usage today",
        "今日已用 Tokens": "Tokens used today",
        "总额度占用": "Total quota usage",
        "推算样本": "Inference samples",
        "窗口用量": "Window usage",
        "推算说明": "Inference notes",
        "重置倒计时": "Reset countdown",
        "未知": "Unknown",
        "即将重置": "Resetting soon",
        "暂无历史数据": "No history data",
        "暂无流量数据": "No traffic data",
        "暂无节点数据": "No node data",
        "该节点暂无认证文件明细": "No auth file details for this node",
        "网关临时不可用，可能是 key-portal 正在重启，3 秒后自动重试...": "Gateway is temporarily unavailable, possibly because key-portal is restarting. Retrying in 3 seconds...",
        "找不到认证文件数据，请刷新页面后重试": "Auth file data was not found. Refresh and try again.",
        "用户统计 - Key Portal": "User Stats - Key Portal",
        "用户使用统计": "User usage stats",
        "点击用户展开 Key，点击 Key 查看当天请求次数和 Token 曲线": "Click a user to expand keys. Click a key to view request and token charts for the day.",
        "Key 类型": "Key type",
        "最后消耗": "Last usage",
        "用量曲线": "Usage chart",
        "北京时间（UTC+8）": "Beijing time (UTC+8)",
        "今天": "Today",
        "7天": "7 days",
        "30天": "30 days",
        "开始日期": "Start date",
        "结束日期": "End date",
        "正在加载曲线...": "Loading chart...",
        "正在加载这个用户的 Key...": "Loading this user's keys...",
        "暂无曲线数据": "No chart data",
        "这个用户当前没有可展示的 Key 数据": "This user has no key data to display",
        "失败请求": "Failed requests",
        "成功请求": "Successful requests",
        "每小时请求数": "Requests per hour",
        "每日请求数": "Daily requests",
        "每月请求数": "Monthly requests",
        "每年请求数": "Yearly requests",
        "平均 Token/小时": "Avg tokens/hour",
        "平均耗时(ms)": "Avg latency (ms)",
        "成功率 / 失败率": "Success / failure rate",
        "当前展开的是累计 Key 用量": "Currently expanded: all-time key usage",
        "使用教程 - AI 能量站": "Setup Guide - AI Access Hub",
        "新用户先按这个顺序走：登录飞书，申请 Key，配置工具，再回来看自己的用量。": "New users should follow this order: sign in with Feishu, create a key, configure the tool, then come back to view usage.",
        "推荐选择": "Recommended choice",
        "Key 分类": "Key categories",
        "配置命令": "Setup commands",
        "常用资源": "Common resources",
        "安全习惯": "Security habits",
        "类型": "Type",
        "模型": "Model",
        "审批": "Approval",
        "建议": "Recommendation",
        "所有 Key Portal 页面都需要登录态，邮箱用于绑定归属。": "All Key Portal pages require sign-in; email is used to bind key ownership.",
        "默认选 GPT Key；Claude 和 DeepSeek 需要审批。": "Choose GPT Key by default. Claude and DeepSeek require approval.",
        "复制“我的 Keys”里的配置命令到终端执行。": "Copy the setup command from My Keys and run it in your terminal.",
        "总览看整体趋势，“我的 Keys”只看自己的明细。": "Overview shows overall trends; My Keys shows only your details.",
        "不要把 Key 发到群里或提交到代码仓库。怀疑泄露时，去“我的 Keys”撤销旧 Key，再重新申请。": "Do not share keys in chat or commit them to code. If you suspect a leak, revoke the old key in My Keys and create a new one.",
        "登录 - Key Portal": "Sign in - Key Portal",
        "Key Portal 已接入飞书登录。请使用公司飞书账号进入页面和报表。": "Key Portal uses Feishu sign-in. Use your company Feishu account to access pages and reports.",
        "统一身份": "Unified identity",
        "飞书邮箱绑定 Key 归属": "Feishu email binds key ownership",
        "状态化访问": "Session-based access",
        "会话保存在服务端": "Sessions are stored server-side",
        "飞书登录": "Feishu sign-in",
        "登录后会回到你刚才访问的页面。历史浏览器 Key 会在登录后自动尝试归属到当前飞书邮箱。": "After sign-in, you will return to the page you were visiting. Historical browser keys will be claimed for the current Feishu email when possible.",
        "使用飞书登录": "Sign in with Feishu",
        "仅 key-portal 页面使用此登录态；管理节点接口仍按原有权限链路处理。": "This session is only for Key Portal pages. Node management APIs still use their existing permission path.",
        "登录失败，请稍后重试": "Sign-in failed. Please try again later.",
        "登录状态已失效，请重新发起飞书登录。": "The sign-in state expired. Start Feishu sign-in again.",
        "登录成功但保存用户信息失败，请联系管理员。": "Sign-in succeeded, but saving user info failed. Contact an admin.",
        "正在跳转飞书...": "Redirecting to Feishu...",
        "无法获取飞书登录地址": "Could not get the Feishu sign-in URL",
        "公司邮箱 *": "Company email *",
        "Key 类型 *": "Key type *",
        "申请理由 *": "Reason *",
        "总额度 (USD) *": "Total budget (USD) *",
        "提交审批申请": "Submit approval request",
        "审批已提交": "Approval submitted",
        "审批请求已发送到飞书，审批通过后将自动生成 Key 并通过飞书消息通知你。": "The approval request was sent to Feishu. After approval, a key will be generated automatically and sent to you in Feishu.",
        "请在飞书中关注审批进度。": "Track approval progress in Feishu.",
        "请输入 @zilliz.com 前面的邮箱部分": "Enter the part before @zilliz.com.",
        "申请该模型组需要填写申请理由": "This model group requires an approval reason.",
        "请填写额度": "Enter a budget.",
        "服务状态 - AI 能量站": "Service Status - AI Access Hub",
        "展示当前节点故障、历史故障和系统公告。页面会自动刷新。": "Shows current node incidents, historical incidents, and system notices. The page refreshes automatically.",
        "当前故障": "Current incidents",
        "历史故障与公告": "Incident history and notices",
        "当前没有进行中的故障": "No active incidents",
        "暂时没有历史故障或公告": "No historical incidents or notices yet",
        "最近检查": "Last checked",
        "刷新状态": "Refresh status",
        "历史事件": "Historical events",
        "配置节点": "Configured nodes",
        "处理说明": "Handling notes",
        "查看原始细节": "View raw details",
        "保存说明": "Save note",
        "保存中...": "Saving...",
        "状态加载失败": "Could not load status",
        "保存失败": "Could not save",
        "开始": "Started",
        "恢复": "Resolved",
        "持续": "Duration",
        "原因": "Reason",
        "影响节点": "Affected nodes",
        "进行中": "Ongoing",
        "已恢复": "Resolved",
        "公告": "Notice",
        "补充故障原因、处理过程或结论": "Add cause, handling steps, or conclusion",
        "申请追加额度": "Request additional budget",
        "追加额度 (USD)": "Additional budget (USD)",
        "请说明为什么需要追加额度": "Explain why you need additional budget",
        "提交审批": "Submit for approval",
        "取消": "Cancel",
        "请填写有效的追加额度": "Enter a valid additional budget.",
        "请填写申请理由": "Enter a reason.",
        "提交失败": "Submission failed",
        "查询失败": "Search failed",
        "请先登录后查看自己的 Keys": "Sign in first to view your keys.",
        "本机历史申请记录": "Local request history",
        "单日": "Single day",
        "累计": "Total",
        "累计+今日": "Total + today",
        "默认": "Default",
        "输入 API Key（如：sk-... 或 usr_pool_...）": "Enter an API key, such as sk-... or usr_pool_...",
        "查询 Key": "Search key",
        "状态": "Status",
        "管理": "Manage",
        "正常": "Normal",
        "停用": "Disable",
        "启用": "Enable",
        "屏蔽飞书告警": "Mute Feishu alerts",
        "近 7 天": "Last 7 days",
        "历史范围": "History range",
        "Tokens 已用": "Tokens used",
        "占集群": "of cluster",
        "占列表": "of list",
        "点击按": "Sort by ",
        "排序": "",
        "导出 CSV": "Export CSV",
        "最近窗口还没有赛车出发。": "No race has started in the recent window yet.",
        "正在加载赛道...": "Loading race track...",
        "申请参赛": "Join race",
        "步行": "Walking",
        "跑步": "Running",
        "自行车": "Bicycle",
        "电动车": "E-bike",
        "汽车": "Car",
        "直升机": "Helicopter",
        "飞机": "Plane",
        "火箭": "Rocket",
        "熄火": "Idle",
        "GPT Key（不限量）": "GPT Key (unlimited)",
        "Claude Key（需审批）": "Claude Key (approval required)",
        "DeepSeek Key（需审批）": "DeepSeek Key (approval required)",
        "只需要填写 @zilliz.com 前面的部分": "Only enter the part before @zilliz.com.",
        "张三": "Alex Zhang",
        "正在读取状态事件...": "Reading status events...",
        "加载中": "Loading",
        "例如：5": "Example: 5",
        "仅移除 apiKeyHelper 和 ANTHROPIC_BASE_URL，不影响其他配置": "Only removes apiKeyHelper and ANTHROPIC_BASE_URL; other settings are not changed.",
        "输出中": "In the output,",
        "后面的就是你的 API Key": "after echo is your API key.",
        "账户": "Account",
        "提交中...": "Submitting...",
        "Bedrock / Claude 按量计费成本较高，请说明必须使用它完成任务的紧急或必要场景；常规用量推荐优先使用 GPT Key。": "Bedrock / Claude pay-as-you-go usage is expensive. Explain why this task urgently or necessarily requires it; for regular usage, prefer GPT Key.",
        "选择赛车人数": "Select racers"
    };

    const PHRASES = [
        ["申请个人 API Key", "Get your personal API key"],
        ["申请个人 Key", "Get a personal key"],
        ["使用教程", "Setup guide"],
        ["查看统计", "View usage"],
        ["剩余可用 Key", "Keys available"],
        ["我申请过的 Keys", "My keys"],
        ["访问 GitHub 仓库", "Open GitHub repo"],
        ["用户用量", "User usage"],
        ["用量报表", "Usage"],
        ["刷新数据", "Refresh"],
        ["申请 API Key", "Get API key"],
        ["贡献 Key", "Contribute a key"],
        ["返回首页", "Home"],
        ["复制移除命令", "Copy removal command"],
        ["复制 Codex 配置命令", "Copy Codex setup command"],
        ["Claude 配置", "Claude setup"],
        ["Codex 配置", "Codex setup"],
        ["查看详情", "Details"],
        ["Key 状态监控", "Key status"],
        ["用户使用统计", "User usage"],
        ["安全移除代理配置", "Remove proxy settings safely"],
        ["公司级 Claude Skills", "Company Claude Skills"]
    ];

    const REPLACEMENTS = [
        [/网络错误[：:]\s*/g, "Network error: "],
        [/请求失败[：:]\s*/g, "Request failed: "],
        [/加载失败[：:]\s*/g, "Could not load: "],
        [/授权失败[：:]\s*/g, "Authorization failed: "],
        [/获取授权链接失败:\s*/g, "Could not get the authorization link: "],
        [/撤销失败[：:]\s*/g, "Could not revoke key: "],
        [/过期时间:\s*/g, "Expires: "],
        [/更新于\s*/g, "Updated "],
        [/(\d+(?:\.\d+)?)h 后过期/g, "$1h left"],
        [/账户:\s*/g, "Account: "],
        [/(\d+)\s*个/g, "$1"],
        [/申请失败/g, "Could not create the key"],
        [/加载中/g, "Loading"],
        [/生成时间[：:]\s*/g, "Generated: "],
        [/北京时间/g, "Beijing time"],
        [/认证文件和流量实时读取节点管理接口/g, "Auth files and traffic are read live from node management APIs"],
        [/页面可见时每\s*(\d+)s\s*刷新/g, "refreshes every $1s while visible"],
        [/数据口径[：:]/g, "Data scope: "],
        [/请求数为加和值/g, "request counts are summed"],
        [/重置\s*/g, "Reset "],
        [/剩余\s*/g, "Remaining "],
        [/已用\s*/g, "Used "],
        [/窗口\s*/g, "Window "],
        [/失败\s*/g, "Failures "],
        [/成功\s*/g, "Success "],
        [/请求\s*/g, "Requests "],
        [/单号范围\s*/g, "single-account range "],
        [/已有\s*/g, "Have "],
        [/额度快照/g, "quota snapshots"],
        [/本地窗口用量不足/g, "insufficient local window usage"],
        [/缺少有效占用百分比/g, "missing valid usage percentage"],
        [/推算/g, "inferred"],
        [/确认/g, "Confirm "],
        [/吗？/g, "?"],
        [/停用后新流量不会再调度到这个认证文件。/g, "New traffic will no longer route to this auth file after disabling."],
        [/启用后该认证文件会重新参与调度。/g, "After enabling, this auth file will participate in routing again."],
        [/健康检查、事件记录和恢复处理会继续执行。/g, "Health checks, event logging, and recovery will continue."],
        [/失败[：:]/g, "failed: "],
        [/进行中/g, "Ongoing"],
        [/已恢复/g, "Resolved"],
        [/公告/g, "Notice"],
        [/当前展开的是/g, "Currently expanded: "],
        [/的 Key 用量/g, " key usage"],
        [/的 Keys/g, "'s keys"],
        [/的用量曲线/g, " usage chart"],
        [/成功[：:]\s*/g, "Success: "],
        [/失败[：:]\s*/g, "Failure: "],
        [/请求数[：:]\s*/g, "Requests: "],
        [/估算[：:]?\s*/g, "Estimated "],
        [/今日\s*/g, "Today "],
        [/累计\s*/g, "Total "],
        [/最后消耗[：:]?/g, "Last usage: "],
        [/创建[：:]?/g, "Created: "],
        [/飞书登录失败[：:]/g, "Feishu sign-in failed: "],
        [/当前总额度/g, "Current total budget"],
        [/追加/g, "additional"],
        [/新预算/g, "new budget"],
        [/未分类/g, "Uncategorized"],
        [/单日/g, "Single day"],
        [/默认/g, "Default"]
    ];

    let currentLang = getInitialLanguage();
    let applying = false;
    const textState = new WeakMap();
    const attrState = new WeakMap();
    const originalAlert = window.alert ? window.alert.bind(window) : null;
    const originalConfirm = window.confirm ? window.confirm.bind(window) : null;

    function getInitialLanguage() {
        try {
            const saved = localStorage.getItem(STORAGE_KEY);
            if (saved === LANG_ZH || saved === LANG_EN) {
                return saved;
            }
        } catch (e) {
            // Ignore storage failures.
        }
        const navLang = (navigator.language || navigator.userLanguage || "").toLowerCase();
        return navLang.startsWith("zh") ? LANG_ZH : LANG_EN;
    }

    function normalize(text) {
        return String(text || "").replace(/\s+/g, " ").trim();
    }

    function translateCore(text) {
        const normalized = normalize(text);
        if (!normalized) {
            return text;
        }
        if (Object.prototype.hasOwnProperty.call(EXACT, normalized)) {
            return EXACT[normalized];
        }
        const exactKeys = Object.keys(EXACT).sort((a, b) => b.length - a.length);
        let translated = normalized;
        for (const key of exactKeys) {
            if (translated.includes(key)) {
                translated = translated.split(key).join(EXACT[key]);
            }
        }
        for (const pair of PHRASES) {
            translated = translated.split(pair[0]).join(pair[1]);
        }
        for (const pair of REPLACEMENTS) {
            translated = translated.replace(pair[0], pair[1]);
        }
        return translated === normalized ? text : translated;
    }

    function translateMessage(text) {
        if (currentLang !== LANG_EN || !text) {
            return text;
        }
        const source = String(text);
        if (!HAN_RE.test(source)) {
            return source;
        }
        const prefix = source.match(/^\s*/)[0];
        const suffix = source.match(/\s*$/)[0];
        const translated = translateCore(source);
        return translated === source ? source : prefix + translated + suffix;
    }

    function shouldSkipTextNode(node) {
        const parent = node.parentElement;
        if (!parent) {
            return true;
        }
        if (SKIP_TAGS.has(parent.tagName)) {
            return true;
        }
        return Boolean(parent.closest(SKIP_CLOSEST));
    }

    function translateTextNode(node) {
        if (shouldSkipTextNode(node)) {
            return;
        }
        const value = node.nodeValue;
        const record = textState.get(node);
        if (currentLang === LANG_ZH) {
            if (record && value !== record.source) {
                node.nodeValue = record.source;
            }
            return;
        }

        let source = value;
        if (record && (value === record.translated || value === record.source)) {
            source = record.source;
        }
        if (!HAN_RE.test(source)) {
            return;
        }
        const translated = translateMessage(source);
        if (translated !== source) {
            textState.set(node, { source, translated });
            if (value !== translated) {
                node.nodeValue = translated;
            }
        }
    }

    function getAttrBucket(el) {
        let bucket = attrState.get(el);
        if (!bucket) {
            bucket = {};
            attrState.set(el, bucket);
        }
        return bucket;
    }

    function translateAttrs(el) {
        for (const attr of ATTRS) {
            if (!el.hasAttribute(attr)) {
                continue;
            }
            translateAttr(el, attr);
        }
        const tag = el.tagName;
        const type = (el.getAttribute("type") || "").toLowerCase();
        if (tag === "INPUT" && ["button", "submit", "reset"].includes(type) && el.hasAttribute("value")) {
            translateAttr(el, "value");
        }
    }

    function translateAttr(el, attr) {
        const value = el.getAttribute(attr);
        const bucket = getAttrBucket(el);
        const record = bucket[attr];
        if (currentLang === LANG_ZH) {
            if (record && value !== record.source) {
                el.setAttribute(attr, record.source);
            }
            return;
        }

        let source = value;
        if (record && (value === record.translated || value === record.source)) {
            source = record.source;
        }
        if (!HAN_RE.test(source)) {
            return;
        }
        const translated = translateMessage(source);
        if (translated !== source) {
            bucket[attr] = { source, translated };
            if (value !== translated) {
                el.setAttribute(attr, translated);
            }
        }
    }

    function walk(root) {
        if (!root) {
            return;
        }
        if (root.nodeType === Node.TEXT_NODE) {
            translateTextNode(root);
            return;
        }
        if (root.nodeType !== Node.ELEMENT_NODE && root.nodeType !== Node.DOCUMENT_NODE && root.nodeType !== Node.DOCUMENT_FRAGMENT_NODE) {
            return;
        }
        if (root.nodeType === Node.ELEMENT_NODE) {
            translateAttrs(root);
        }
        const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT | NodeFilter.SHOW_ELEMENT);
        let node = walker.nextNode();
        while (node) {
            if (node.nodeType === Node.TEXT_NODE) {
                translateTextNode(node);
            } else if (node.nodeType === Node.ELEMENT_NODE) {
                translateAttrs(node);
            }
            node = walker.nextNode();
        }
    }

    function applyDocument() {
        if (!document.body) {
            return;
        }
        applying = true;
        document.documentElement.lang = currentLang === LANG_EN ? "en" : "zh-CN";
        walk(document.documentElement);
        updateToggleState();
        applying = false;
    }

    function injectStyles() {
        if (document.getElementById("keyPortalI18nStyles")) {
            return;
        }
        const style = document.createElement("style");
        style.id = "keyPortalI18nStyles";
        style.textContent = `
            .kp-lang-toggle {
                position: fixed;
                top: 14px;
                right: 14px;
                z-index: 2147483000;
                display: inline-flex;
                gap: 2px;
                padding: 4px;
                border-radius: 999px;
                background: rgba(17, 24, 39, 0.78);
                box-shadow: 0 10px 30px rgba(0, 0, 0, 0.22);
                backdrop-filter: blur(12px);
            }
            .kp-lang-toggle.in-shell {
                position: static;
                z-index: auto;
                background: #f1f5f9;
                border: 1px solid #d9e1ec;
                box-shadow: none;
                backdrop-filter: none;
                flex: 0 0 auto;
            }
            .kp-lang-toggle button {
                border: 0;
                border-radius: 999px;
                padding: 7px 11px;
                background: transparent;
                color: rgba(255, 255, 255, 0.78);
                cursor: pointer;
                font-size: 12px;
                font-weight: 700;
                letter-spacing: 0.01em;
            }
            .kp-lang-toggle button.active {
                background: #fff;
                color: #2f3a8f;
            }
            .kp-lang-toggle.in-shell button {
                color: #64748b;
                padding: 5px 9px;
            }
            .kp-lang-toggle.in-shell button.active {
                color: #0f766e;
                box-shadow: 0 1px 3px rgba(15, 23, 42, 0.08);
            }
            @media (max-width: 640px) {
                .kp-lang-toggle {
                    top: 10px;
                    right: 10px;
                    transform: scale(0.92);
                    transform-origin: top right;
                }
            }
        `;
        document.head.appendChild(style);
    }

    function injectToggle() {
        if (!document.body) {
            return;
        }
        const shellTarget = document.querySelector(SHELL_TOGGLE_SELECTOR);
        let wrap = document.getElementById("keyPortalLangToggle");
        if (!wrap) {
            wrap = document.createElement("div");
            wrap.id = "keyPortalLangToggle";
            wrap.innerHTML = `
                <button type="button" data-lang="${LANG_ZH}">中文</button>
                <button type="button" data-lang="${LANG_EN}">EN</button>
            `;
            wrap.addEventListener("click", (event) => {
                const btn = event.target.closest("button[data-lang]");
                if (!btn) {
                    return;
                }
                setLanguage(btn.getAttribute("data-lang"));
            });
        }
        wrap.className = "kp-lang-toggle" + (shellTarget ? " in-shell" : "");
        if ((shellTarget || document.body) !== wrap.parentElement) {
            (shellTarget || document.body).appendChild(wrap);
        }
        updateToggleState();
    }

    function updateToggleState() {
        const wrap = document.getElementById("keyPortalLangToggle");
        if (!wrap) {
            return;
        }
        wrap.querySelectorAll("button[data-lang]").forEach((btn) => {
            btn.classList.toggle("active", btn.getAttribute("data-lang") === currentLang);
        });
    }

    function setLanguage(lang) {
        if (lang !== LANG_ZH && lang !== LANG_EN) {
            return;
        }
        currentLang = lang;
        try {
            localStorage.setItem(STORAGE_KEY, lang);
        } catch (e) {
            // Ignore storage failures.
        }
        applyDocument();
        rerenderCharts();
        window.dispatchEvent(new CustomEvent("keyPortalLanguageChange", { detail: { lang } }));
    }

    function translateChartConfig(value) {
        if (currentLang !== LANG_EN || !value) {
            return value;
        }
        if (typeof value === "string") {
            return translateMessage(value);
        }
        if (Array.isArray(value)) {
            for (let i = 0; i < value.length; i += 1) {
                value[i] = translateChartConfig(value[i]);
            }
            return value;
        }
        if (typeof value === "object") {
            Object.keys(value).forEach((key) => {
                value[key] = translateChartConfig(value[key]);
            });
        }
        return value;
    }

    function patchChart() {
        const OriginalChart = window.Chart;
        if (!OriginalChart || OriginalChart.__keyPortalI18nPatched) {
            return;
        }
        function I18nChart(ctx, config) {
            translateChartConfig(config);
            return new OriginalChart(ctx, config);
        }
        Object.setPrototypeOf(I18nChart, OriginalChart);
        I18nChart.prototype = OriginalChart.prototype;
        I18nChart.__keyPortalI18nPatched = true;
        I18nChart.__OriginalChart = OriginalChart;
        window.Chart = I18nChart;
    }

    function rerenderCharts() {
        if (typeof window.renderChart === "function") {
            try {
                window.renderChart();
            } catch (e) {
                // Chart rerendering is best-effort.
            }
        }
    }

    function installMutationObserver() {
        const observer = new MutationObserver((mutations) => {
            if (applying) {
                return;
            }
            applying = true;
            for (const mutation of mutations) {
                if (mutation.type === "childList") {
                    mutation.addedNodes.forEach((node) => walk(node));
                    if (document.querySelector(SHELL_TOGGLE_SELECTOR)) {
                        injectToggle();
                    }
                } else if (mutation.type === "characterData") {
                    translateTextNode(mutation.target);
                } else if (mutation.type === "attributes") {
                    translateAttrs(mutation.target);
                }
            }
            updateToggleState();
            applying = false;
        });
        observer.observe(document.documentElement, {
            childList: true,
            characterData: true,
            attributes: true,
            subtree: true,
            attributeFilter: ATTRS.concat(["value"])
        });
    }

    if (originalAlert) {
        window.alert = function (message) {
            return originalAlert(translateMessage(message));
        };
    }
    if (originalConfirm) {
        window.confirm = function (message) {
            return originalConfirm(translateMessage(message));
        };
    }

    window.keyPortalI18n = {
        setLanguage,
        getLanguage: () => currentLang,
        t: translateMessage
    };

    patchChart();
    const chartPatchTimer = window.setInterval(() => {
        patchChart();
        if (window.Chart && window.Chart.__keyPortalI18nPatched) {
            window.clearInterval(chartPatchTimer);
        }
    }, 100);
    window.setTimeout(() => window.clearInterval(chartPatchTimer), 5000);

    document.addEventListener("DOMContentLoaded", () => {
        injectStyles();
        injectToggle();
        applyDocument();
        installMutationObserver();
    });
})();
