(function () {
  const LS_KEY = 'quant_live_quotes';

  function todayStr() {
    // YYYY-MM-DD in all locales
    return new Date().toLocaleDateString('sv-SE');
  }

  function getDate(quotes) {
    if (!quotes) return null;
    const meta = quotes.__meta__;
    if (meta && /^\d{4}-\d{2}-\d{2}/.test(String(meta.date || '')))
      return String(meta.date).slice(0, 10);
    for (const key of Object.keys(quotes)) {
      if (key === '__meta__') continue;
      const q = quotes[key];
      if (q && /^\d{4}-\d{2}-\d{2}/.test(String(q.date || '')))
        return String(q.date).slice(0, 10);
    }
    return null;
  }

  function save(quotes) {
    try { localStorage.setItem(LS_KEY, JSON.stringify(quotes)); } catch (_) {}
  }

  // Returns today's cached quotes, or null if absent / stale.
  function load() {
    try {
      const raw = localStorage.getItem(LS_KEY);
      if (!raw) return null;
      const quotes = JSON.parse(raw);
      return getDate(quotes) === todayStr() ? quotes : null;
    } catch (_) { return null; }
  }

  // Fetches fresh quotes from server, saves on success.
  async function fetchFresh(forceRefresh = false) {
    try {
      const quotes = await fetch('/api/quotes' + (forceRefresh ? '?refresh=1' : ''))
        .then(r => r.json());
      const result = quotes || {};
      if (getDate(result)) save(result);
      return result;
    } catch (_) { return {}; }
  }

  window.QuotesCache = { save, load, fetchFresh, getDate };
})();
