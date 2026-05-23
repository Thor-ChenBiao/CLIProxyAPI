(function () {
    const navItems = [
        { href: '/', label: '总览' },
        { href: '/my-keys', label: '我的 Keys' },
        { href: '/guide', label: '使用教程' },
        { href: '/status', label: '服务状态' },
    ];
    const adminItems = [
        { href: '/admin/users', label: '用户用量' },
        { href: '/admin/auth-stats', label: '认证统计' },
        { href: '/litellm/ui/', label: 'LiteLLM 后台', external: true },
    ];

    function escapeHtml(value) {
        return String(value || '')
            .replace(/&/g, '&amp;')
            .replace(/</g, '&lt;')
            .replace(/>/g, '&gt;')
            .replace(/"/g, '&quot;')
            .replace(/'/g, '&#39;');
    }

    function initials(user, email) {
        const text = String(user?.name || email || 'U').trim();
        const first = Array.from(text)[0] || 'U';
        return first.toUpperCase();
    }

    function isActive(href) {
        const path = window.location.pathname || '/';
        if (href === '/') return path === '/';
        return path === href || path.startsWith(href + '/');
    }

    function navLink(item, className) {
        const active = isActive(item.href) ? ' active' : '';
        const target = item.external ? ' target="_blank" rel="noopener noreferrer"' : '';
        return `<a class="portal-nav-link ${className || ''}${active}" href="${item.href}"${target}>${escapeHtml(item.label)}</a>`;
    }

    async function logout() {
        try {
            await fetch('/api/logout', { method: 'POST' });
        } finally {
            window.location.href = '/login';
        }
    }

    function mount(session) {
        const user = session.user || {};
        const email = session.email || user.email || '';
        const role = session.is_admin ? 'Admin' : 'User';
        const avatar = user.avatar_url
            ? `<img src="${escapeHtml(user.avatar_url)}" alt="">`
            : escapeHtml(initials(user, email));
        const adminNav = session.is_admin ? adminItems.map(item => navLink(item, 'admin')).join('') : '';

        document.body.classList.add('portal-shell-ready');
        if (session.is_admin) document.body.classList.add('portal-is-admin');

        const bar = document.createElement('header');
        bar.className = 'portal-topbar';
        bar.innerHTML = `
            <div class="portal-topbar-inner">
                <a class="portal-brand" href="/">AI 能量站</a>
                <nav class="portal-nav" aria-label="主导航">
                    ${navItems.map(item => navLink(item)).join('')}
                    ${adminNav}
                </nav>
                <div class="portal-user">
                    <div class="portal-avatar">${avatar}</div>
                    <div class="portal-user-text">
                        <div class="portal-user-name">${escapeHtml(user.name || email || '已登录')}</div>
                        <div class="portal-user-email">${escapeHtml(email)}</div>
                    </div>
                    <span class="portal-role">${role}</span>
                    <button class="portal-logout" type="button">退出</button>
                </div>
            </div>
        `;
        bar.querySelector('.portal-logout').addEventListener('click', logout);
        document.body.prepend(bar);
    }

    async function init() {
        try {
            const resp = await fetch('/api/session', { headers: { Accept: 'application/json' } });
            const session = await resp.json();
            if (!session.authenticated) {
                window.location.href = '/login?next=' + encodeURIComponent(window.location.pathname + window.location.search);
                return;
            }
            mount(session);
        } catch (err) {
            console.error('Failed to mount portal shell:', err);
        }
    }

    document.addEventListener('DOMContentLoaded', init);
})();
