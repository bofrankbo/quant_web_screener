(function () {
  const NAV_ITEMS = [
    { key: 'home', label: 'Home', href: '/' },
    { key: 'market', label: 'Market', href: '/market-overview' },
    { key: 'screener', label: 'Screener', href: '/backtest' },
    { key: 'watchlist', label: 'Watchlist', href: '/watchlist-manager' },
    { key: 'portfolio', label: 'Portfolio', href: '/portfolio' },
    { key: 'draw', label: 'Pattern Draw', href: '/draw' },
  ];

  function resolvePageKey(host) {
    return host?.dataset?.page
      || host?.dataset?.topbarPage
      || document.body?.dataset?.topbarPage
      || 'home';
  }

  function resolvePageTitle(pageKey, host) {
    const customTitle = host?.dataset?.title
      || host?.dataset?.topbarTitle
      || document.body?.dataset?.topbarTitle;
    if (customTitle) return customTitle;
    const fallback = NAV_ITEMS.find(item => item.key === pageKey);
    return fallback ? fallback.label : 'Quant Backtest';
  }

  function buildNav(pageKey) {
    return NAV_ITEMS.map(item => {
      const active = item.key === pageKey ? ' active' : '';
      return `<a class="app-nav-link${active}" href="${item.href}">${item.label}</a>`;
    }).join('');
  }

  function buildSlot(host) {
    const template = host.querySelector('template[data-topbar-slot]');
    if (!template) return '';
    return template.innerHTML.trim();
  }

  function shouldHideAuth(host) {
    return (host?.dataset?.topbarAuth || '').toLowerCase() === 'hide';
  }

  function syncTopbarHeight(host) {
    const update = () => {
      const h = Math.max(54, Math.round(host.getBoundingClientRect().height || 54));
      document.documentElement.style.setProperty('--app-topbar-height', `${h}px`);
    };

    update();

    if (window.ResizeObserver) {
      if (window.__appTopbarObserver) {
        window.__appTopbarObserver.disconnect();
      }
      window.__appTopbarObserver = new ResizeObserver(update);
      window.__appTopbarObserver.observe(host);
    } else {
      window.addEventListener('resize', update);
    }
  }

  async function hydrateAuth(slot) {
    if (!slot) return;
    try {
      const resp = await fetch('/auth/me');
      if (resp.ok) {
        const data = await resp.json();
        const user = data.user || {};
        const label = user.name || user.email || 'Signed in';
        slot.innerHTML = `<a class="app-auth-chip" href="#" data-auth-logout>${label}</a>`;
        const logout = slot.querySelector('[data-auth-logout]');
        if (logout) {
          logout.addEventListener('click', async (e) => {
            e.preventDefault();
            await fetch('/auth/logout', { method: 'POST' });
            window.location.reload();
          });
        }
        return;
      }
    } catch (err) {
      console.error('auth status failed:', err);
    }

    slot.innerHTML = '<a class="app-auth-link" href="/login">Sign in</a>';
  }

  function renderTopbar() {
    const host = document.getElementById('app-topbar');
    if (!host) return;

    const pageKey = resolvePageKey(host);
    const pageTitle = resolvePageTitle(pageKey, host);
    const slotHTML = buildSlot(host);
    const hideAuth = shouldHideAuth(host);

    host.className = 'app-topbar';
    host.innerHTML = `
      <a class="app-brand" href="/">
        <span class="app-brand-mark" aria-hidden="true"></span>
        <span class="app-brand-copy">
          <span class="app-brand-name">Quant Backtest</span>
          <span class="app-brand-page">${pageTitle}</span>
        </span>
      </a>
      <div class="app-topbar-slot">${slotHTML}</div>
      <nav class="app-nav" aria-label="Primary">
        ${buildNav(pageKey)}
      </nav>
      ${hideAuth ? '' : '<div class="app-auth" data-auth-slot></div>'}
    `;

    syncTopbarHeight(host);
    if (!hideAuth) {
      hydrateAuth(host.querySelector('[data-auth-slot]'));
    }
  }

  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', renderTopbar);
  } else {
    renderTopbar();
  }

  window.AppTopbar = { render: renderTopbar };
})();
