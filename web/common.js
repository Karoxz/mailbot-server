// MailBot web dashboard — shared across every page. Plain JS, no
// build step, no framework. Include this BEFORE any page-specific
// script (it sets up window.MailBot with everything a page needs).

(function () {
  const STORAGE_KEY = "mailbot_license_key";
  const THEME_KEY = "mailbot_theme";
  const FETCH_TIMEOUT_MS = 12000;

  // ── Global error visibility — see app.js's original comment: this
  // must never fail silently, on any page. ──────────────────────────
  function showFatalError(msg) {
    let banner = document.getElementById("fatal-error-banner");
    if (!banner) {
      banner = document.createElement("div");
      banner.id = "fatal-error-banner";
      banner.style.cssText =
        "position:fixed;top:0;left:0;right:0;z-index:9999;background:#dc2626;" +
        "color:#fff;padding:12px 16px;font:13px monospace;white-space:pre-wrap;" +
        "word-break:break-word;";
      document.body.prepend(banner);
    }
    banner.textContent = "Plutus Bot error: " + msg;
  }
  window.addEventListener("error", (e) =>
    showFatalError(`${e.message} (${e.filename}:${e.lineno})`));
  window.addEventListener("unhandledrejection", (e) =>
    showFatalError(String(e.reason)));

  // ── Auth ───────────────────────────────────────────────────────────
  function getLicenseKey() {
    try { return localStorage.getItem(STORAGE_KEY); } catch (e) { return null; }
  }
  function setLicenseKey(key) {
    try { localStorage.setItem(STORAGE_KEY, key); } catch (e) {}
  }
  function clearLicenseKey() {
    try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
  }
  // Call at the top of every page except login.html. Redirects to
  // login if there's no stored key — pages never render without one.
  function requireAuth() {
    const key = getLicenseKey();
    if (!key) {
      window.location.href = "login.html";
      return null;
    }
    return key;
  }

  // ── Theme ──────────────────────────────────────────────────────────
  function applyTheme(theme) {
    if (theme === "light") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", "dark");
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = theme === "light" ? "🌙" : "☀️";
  }
  function initTheme() {
    let stored = null;
    try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
    applyTheme(stored === "light" ? "light" : "dark");
  }
  function toggleTheme() {
    const isDark = document.documentElement.getAttribute("data-theme") === "dark";
    const next = isDark ? "light" : "dark";
    applyTheme(next);
    try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
  }

  // ── API helpers ──────────────────────────────────────────────────
  async function timedFetch(url, opts = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
    try {
      return await fetch(url, { ...opts, signal: controller.signal });
    } catch (err) {
      if (err.name === "AbortError") {
        throw new Error("Request timed out — check your connection and try again.");
      }
      throw err;
    } finally {
      clearTimeout(timer);
    }
  }
  async function handleJsonResponse(res) {
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      const err = new Error(body.detail || `HTTP ${res.status}`);
      err.status = res.status;
      throw err;
    }
    return res.json();
  }
  async function apiGet(path, params = {}) {
    const usp = new URLSearchParams({ license_key: getLicenseKey(), ...params });
    const res = await timedFetch(`${path}?${usp.toString()}`);
    return handleJsonResponse(res);
  }
  async function apiSend(method, path, body) {
    const res = await timedFetch(path, {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ license_key: getLicenseKey(), ...body }),
    });
    return handleJsonResponse(res);
  }
  const apiPost = (path, body) => apiSend("POST", path, body);
  const apiPatch = (path, body) => apiSend("PATCH", path, body);
  async function apiDelete(path, params = {}) {
    const usp = new URLSearchParams({ license_key: getLicenseKey(), ...params });
    const res = await timedFetch(`${path}?${usp.toString()}`, { method: "DELETE" });
    return handleJsonResponse(res);
  }

  // If any API call fails specifically because the license itself is
  // bad (not a network blip), bounce to login rather than showing a
  // dashboard full of failed panels.
  function bounceIfAuthError(err) {
    if (err && err.status === 403) {
      clearLicenseKey();
      window.location.href = "login.html";
      return true;
    }
    return false;
  }

  // ── Formatters ─────────────────────────────────────────────────────
  function esc(s) {
    const d = document.createElement("div");
    d.textContent = s ?? "";
    return d.innerHTML;
  }
  function fmtMoney(n) {
    if (n === null || n === undefined) return "—";
    return `$${Number(n).toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
  }
  function fmtRate(n) {
    if (n === null || n === undefined) return "—";
    return `$${Number(n).toFixed(2)}`;
  }
  function fmtPct(n) {
    if (n === null || n === undefined) return "—";
    return `${Math.round(n * 100)}%`;
  }
  function fmtWhen(iso) {
    if (!iso) return "—";
    try {
      return new Date(iso).toLocaleString(undefined, {
        month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      });
    } catch (e) { return iso; }
  }

  // ── Nav ────────────────────────────────────────────────────────────
  const NAV_ITEMS = [
    { href: "index.html",    label: "Dashboard" },
    { href: "loads.html",    label: "Loads" },
    { href: "trucks.html",   label: "Trucks" },
    { href: "brokers.html",  label: "Brokers" },
    { href: "settings.html", label: "Settings" },
  ];
  function renderTopbar(activeHref) {
    const mount = document.getElementById("app-nav");
    if (!mount) return;
    const links = NAV_ITEMS.map((item) => {
      const active = item.href === activeHref ? ' class="active"' : "";
      return `<a href="${item.href}"${active}>${item.label}</a>`;
    }).join("");
    mount.innerHTML = `
      <a href="index.html" class="brand">
        <img src="assets/plutus_logo.jpg" alt="" class="brand-logo">
        Plutus Bot
      </a>
      <nav class="topnav">${links}</nav>
      <div class="topbar-actions">
        <span id="conn-dot" class="dot" title="Connection status"></span>
        <button id="theme-toggle" class="icon-btn" title="Toggle theme" type="button">🌙</button>
        <button id="logout-btn" class="icon-btn" title="Log out" type="button">⎋</button>
      </div>`;
    document.getElementById("theme-toggle").addEventListener("click", toggleTheme);
    document.getElementById("logout-btn").addEventListener("click", () => {
      clearLicenseKey();
      window.location.href = "login.html";
    });
  }
  function setConn(ok) {
    const dot = document.getElementById("conn-dot");
    if (!dot) return;
    dot.classList.toggle("ok", ok);
    dot.classList.toggle("bad", !ok);
  }

  window.MailBot = {
    getLicenseKey, setLicenseKey, clearLicenseKey, requireAuth,
    initTheme, toggleTheme,
    apiGet, apiPost, apiPatch, apiDelete, bounceIfAuthError,
    esc, fmtMoney, fmtRate, fmtPct, fmtWhen,
    renderTopbar, setConn, showFatalError,
  };
})();
