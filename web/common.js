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
  // Two logo variants exist because the mark itself is gold-on-black vs
  // gold-on-white, not just a filter/invert — plutus_logo.jpg (dark bg)
  // and plutus_logo_light.jpg (light bg), both real assets, not derived.
  function logoSrc(theme) {
    return theme === "light" ? "assets/plutus_logo_light.jpg" : "assets/plutus_logo.jpg";
  }
  function applyTheme(theme) {
    if (theme === "light") document.documentElement.removeAttribute("data-theme");
    else document.documentElement.setAttribute("data-theme", "dark");
    const btn = document.getElementById("theme-toggle");
    if (btn) btn.textContent = theme === "light" ? "🌙" : "☀️";

    const src = logoSrc(theme);
    document.querySelectorAll(".brand-logo, .login-logo").forEach((img) => { img.src = src; });
    const favicon = document.querySelector('link[rel="icon"]');
    if (favicon) favicon.href = src;
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

  // ── Gmail thread lookup (Slice 3, option C — see roadmap) ───────────
  // No real Gmail thread ID reaches the server today (/api/parse always
  // hands process_bid_email a placeholder message with no real
  // threadId), so an exact #all/<threadId> deep link isn't available.
  // A Gmail SEARCH link is the automated alternative: order numbers are
  // distinctive enough that this reliably surfaces the right thread as
  // the top result, with zero new infrastructure needed. Upgrades
  // transparently to a real thread link automatically once/if a real
  // thread_id ever does reach the frontend (checked first, unused today).
  function gmailSearchUrl(orderId, brokerEmail, threadId) {
    if (threadId) return `https://mail.google.com/mail/u/0/#all/${threadId}`;
    let q = String(orderId || "").trim();
    if (brokerEmail) q += ` from:${brokerEmail}`;
    return `https://mail.google.com/mail/u/0/#search/${encodeURIComponent(q)}`;
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

  // ── Diff-render (Slice 3, animation/fluidity pass) ──────────────────
  // Every polling list (renderFeed/renderBidHistory/renderLoads) used to
  // blow away and rebuild its whole innerHTML on every 10s refresh — the
  // reason the UI read as static/flat rather than alive, and why nothing
  // could animate in (a freshly-innerHTML'd node has no "before" state to
  // transition from). This keeps DOM nodes alive across refreshes: an
  // item whose key persists keeps its actual element (no animation
  // replay, no lost focus/selection), a genuinely new key gets a fresh
  // element with .enter-anim so it visibly slides/fades in, and a key
  // that's gone this round is removed. Content updates in place via a
  // cheap signature check so unchanged rows aren't touched at all.
  //
  // container: the element whose children ARE the list (e.g. <tbody>,
  //   a <div class="feed-list">).
  // items: the new data array.
  // opts.key(item)      -> stable string/number identifying the item.
  // opts.html(item)     -> the item's *outer* HTML (the <tr>/<div>...tag
  //   included) as a string. Must NOT include a data-key attribute —
  //   diffRender stamps that itself so callers can't forget it.
  // opts.isRow           -> true if container is a <tbody> (so element
  //   creation happens inside a <table><tbody> for browser parsing rules).
  // opts.emptyHtml       -> full HTML to show when items is empty/absent.
  function _hashStr(s) {
    let h = 0;
    for (let i = 0; i < s.length; i++) h = (h * 31 + s.charCodeAt(i)) | 0;
    return h;
  }
  function _htmlToElement(html, isRow) {
    const wrap = document.createElement(isRow ? "table" : "div");
    wrap.innerHTML = isRow ? `<tbody>${html.trim()}</tbody>` : html.trim();
    return isRow ? wrap.querySelector("tbody").firstElementChild : wrap.firstElementChild;
  }
  function diffRender(container, items, opts) {
    if (!items || items.length === 0) {
      if (container.dataset.mbEmpty !== "1") {
        container.innerHTML = opts.emptyHtml;
        container.dataset.mbEmpty = "1";
      }
      return true; // rendered empty state
    }
    container.dataset.mbEmpty = "0";
    const existing = new Map();
    Array.from(container.children).forEach((el) => {
      if (el.dataset && el.dataset.key) existing.set(el.dataset.key, el);
    });
    const frag = document.createDocumentFragment();
    let anyNew = false;
    items.forEach((item) => {
      const key = String(opts.key(item));
      const html = opts.html(item);
      const sig = String(_hashStr(html));
      let el = existing.get(key);
      if (el) {
        existing.delete(key);
        if (el.dataset.mbSig !== sig) {
          const fresh = _htmlToElement(html, opts.isRow);
          el.innerHTML = fresh.innerHTML;
          el.dataset.mbSig = sig;
        }
      } else {
        el = _htmlToElement(html, opts.isRow);
        el.dataset.key = key;
        el.dataset.mbSig = sig;
        el.classList.add("enter-anim");
        anyNew = true;
      }
      frag.appendChild(el);
    });
    existing.forEach((el) => el.remove()); // whatever's left was dropped this round
    container.innerHTML = "";
    container.appendChild(frag);
    return anyNew;
  }

  // Skeleton placeholders — swapped in for the initial "Loading…" state
  // of any list/table so the page reads as alive immediately rather than
  // frozen, and there's a real "before" for the first real diffRender
  // pass to transition from.
  function skeletonCards(n = 3) {
    return Array.from({ length: n }, () => `
      <div class="skeleton-card">
        <div class="skeleton-line" style="width:40%;"></div>
        <div class="skeleton-line" style="width:70%;"></div>
      </div>`).join("");
  }
  function skeletonRows(cols, n = 4) {
    const cells = Array.from({ length: cols }, () =>
      `<td><div class="skeleton-line" style="width:${60 + Math.round(Math.random() * 30)}%;"></div></td>`
    ).join("");
    return Array.from({ length: n }, () => `<tr class="skeleton-row">${cells}</tr>`).join("");
  }

  // .enter-anim uses animation-fill-mode:both, which means the animated
  // property (opacity) stays pinned to the keyframe's end value for as
  // long as the class is present — that would permanently fight any
  // other opacity styling on the same element (e.g. a dimmed blacklisted-
  // broker row) even after the animation visibly finishes. Strip the
  // class the moment it completes so normal CSS regains control.
  document.addEventListener("animationend", (e) => {
    if (e.animationName === "fadeInUp") e.target.classList.remove("enter-anim");
  });

  // Re-triggers the .enter-anim keyframe on an element that's about to
  // become visible (e.g. an add/edit form panel toggled via `hidden`) —
  // for panels whose *open* transition matters but whose close doesn't
  // need to animate. Forces a reflow so the class re-applies even if it
  // was already present (e.g. the panel was opened and closed rapidly
  // before the previous animationend cleanup ran).
  function popIn(el) {
    el.classList.remove("enter-anim");
    void el.offsetWidth;
    el.classList.add("enter-anim");
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
    const currentTheme = document.documentElement.getAttribute("data-theme") === "dark" ? "dark" : "light";
    mount.innerHTML = `
      <a href="index.html" class="brand">
        <img src="${logoSrc(currentTheme)}" alt="" class="brand-logo">
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
    renderTopbar, setConn, showFatalError, gmailSearchUrl,
    diffRender, skeletonCards, skeletonRows, popIn,
  };
})();
