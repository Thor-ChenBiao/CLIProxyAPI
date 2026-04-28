(function () {
    "use strict";

    const STORAGE_KEY = "keyPortalLang";
    const LANG_ZH = "zh";
    const LANG_EN = "en";
    const HAN_RE = /[\u3400-\u9fff]/;
    const SKIP_TAGS = new Set(["SCRIPT", "STYLE", "NOSCRIPT", "TEXTAREA"]);
    const SKIP_CLOSEST = "pre, code";
    const ATTRS = ["placeholder", "title", "aria-label"];

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
        "看到这个提示说明配置成功": "This means the setup loaded correctly"
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
        [/加载中/g, "Loading"]
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
        let translated = normalized;
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
        if (!document.body || document.getElementById("keyPortalLangToggle")) {
            return;
        }
        const wrap = document.createElement("div");
        wrap.id = "keyPortalLangToggle";
        wrap.className = "kp-lang-toggle";
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
        document.body.appendChild(wrap);
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
