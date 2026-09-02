// MailBot web dashboard — plain JS, no build step, no framework.
// Talks to /api/web/* on the same origin (Caddy already routes the
// whole domain to this FastAPI server, so relative URLs just work).

// Surface ANY uncaught error directly on the page instead of failing
// silently — "nothing happens" is the hardest kind of bug to debug
// remotely, so make sure it can never look like that again.
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
  banner.textContent = "MailBot dashboard error: " + msg;
}
window.addEventListener("error", (e) => showFatalError(e.message + " (" + e.filename + ":" + e.lineno + ")"));
window.addEventListener("unhandledrejection", (e) => showFatalError(String(e.reason)));

const API_BASE = "";
const STORAGE_KEY = "mailbot_license_key";
const THEME_KEY = "mailbot_theme";
const POLL_MS = 10000;

const $ = (sel) => document.querySelector(sel);

const el = {
  loginScreen: $("#login-screen"),
  dashScreen: $("#dashboard-screen"),
  loginForm: $("#login-form"),
  loginKey: $("#login-key"),
  loginError: $("#login-error"),
  connDot: $("#conn-dot"),
  themeToggle: $("#theme-toggle"),
  logoutBtn: $("#logout-btn"),
  statTotal: $("#stat-total"),
  statWinrate: $("#stat-winrate"),
  statRate: $("#stat-rate"),
  statPending: $("#stat-pending"),
  feedList: $("#feed-list"),
  bidBody: $("#bid-table-body"),
};

let licenseKey = null;
let pollTimer = null;

// ── Theme ──────────────────────────────────────────────────────────
function applyTheme(theme) {
  if (theme === "light") {
    document.documentElement.removeAttribute("data-theme");
    el.themeToggle.textContent = "🌙";
  } else {
    document.documentElement.setAttribute("data-theme", "dark");
    el.themeToggle.textContent = "☀️";
  }
}

function initTheme() {
  let stored = null;
  try { stored = localStorage.getItem(THEME_KEY); } catch (e) {}
  applyTheme(stored === "light" ? "light" : "dark"); // dark default
}

el.themeToggle.addEventListener("click", () => {
  const isDark = document.documentElement.getAttribute("data-theme") === "dark";
  const next = isDark ? "light" : "dark";
  applyTheme(next);
  try { localStorage.setItem(THEME_KEY, next); } catch (e) {}
});

// ── API helper ─────────────────────────────────────────────────────
// AbortController timeout so a stalled connection (flaky wifi/mobile
// data, a firewall silently dropping the request) surfaces as a clear
// error instead of hanging forever — which would look exactly like
// "nothing happens" to whoever's staring at the button.
const FETCH_TIMEOUT_MS = 12000;

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

async function apiGet(path, params = {}) {
  const usp = new URLSearchParams({ license_key: licenseKey, ...params });
  const res = await timedFetch(`${API_BASE}${path}?${usp.toString()}`);
  if (!res.ok) {
    const body = await res.json().catch(() => ({}));
    throw new Error(body.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

async function apiPost(path, body) {
  const res = await timedFetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const err = await res.json().catch(() => ({}));
    throw new Error(err.detail || `HTTP ${res.status}`);
  }
  return res.json();
}

// ── Login ──────────────────────────────────────────────────────────
el.loginForm.addEventListener("submit", async (e) => {
  e.preventDefault();
  const key = el.loginKey.value.trim();
  if (!key) return;
  el.loginError.hidden = true;
  const btn = el.loginForm.querySelector("button");
  const originalLabel = btn.textContent;
  btn.disabled = true;
  btn.textContent = "Logging in…";
  try {
    await apiPost("/api/web/login", { license_key: key });
    licenseKey = key;
    try { localStorage.setItem(STORAGE_KEY, key); } catch (err) {}
    showDashboard();
  } catch (err) {
    el.loginError.textContent = err.message || "Login failed.";
    el.loginError.hidden = false;
  } finally {
    btn.disabled = false;
    btn.textContent = originalLabel;
  }
});

el.logoutBtn.addEventListener("click", () => {
  licenseKey = null;
  try { localStorage.removeItem(STORAGE_KEY); } catch (e) {}
  stopPolling();
  el.dashScreen.hidden = true;
  el.loginScreen.hidden = false;
  el.loginKey.value = "";
  el.loginKey.focus();
});

function showDashboard() {
  el.loginScreen.hidden = true;
  el.dashScreen.hidden = false;
  refreshAll();
  startPolling();
}

// ── Rendering ──────────────────────────────────────────────────────
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
    const d = new Date(iso);
    return d.toLocaleString(undefined, {
      month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
    });
  } catch (e) { return iso; }
}
function esc(s) {
  const d = document.createElement("div");
  d.textContent = s ?? "";
  return d.innerHTML;
}

function renderStats(stats) {
  el.statTotal.textContent = stats.total_bids ?? "—";
  el.statWinrate.textContent = fmtPct(stats.win_rate);
  el.statRate.textContent = fmtRate(stats.avg_rate_per_mile);
  el.statPending.textContent = stats.pending ?? "—";
}

function renderFeed(items) {
  if (!items || items.length === 0) {
    el.feedList.innerHTML = '<p class="empty-note">No matched loads yet.</p>';
    return;
  }
  el.feedList.innerHTML = items.map((it) => {
    const decision = it.load_decision?.decision;
    const decisionBadge = decision
      ? `<span class="badge decision-${decision.toLowerCase()}">${esc(decision)}</span>`
      : "";
    const miles = it.google_deadhead != null ? `${it.google_deadhead}mi deadhead` : "";
    const eta = it.deadhead_eta_minutes != null ? `${Math.round(it.deadhead_eta_minutes / 60)}h ETA` : "";
    const suggested = it.bid_recommendation?.suggested_amount
      ? `Suggested ${fmtMoney(it.bid_recommendation.suggested_amount)}`
      : "";
    const meta = [it.driver_name, miles, eta, suggested].filter(Boolean).map(esc).join(" · ");
    return `
      <div class="feed-card">
        <div class="order">#${esc(it.order)} <span style="color:var(--text-dim);font-weight:400;">${esc(it.vehicle_required || "")}</span></div>
        ${decisionBadge}
        <div class="route">${esc(it.pickup_loc || "?")} → ${esc(it.delivery_loc || "?")}</div>
        ${meta ? `<div class="meta">${meta}</div>` : ""}
      </div>`;
  }).join("");
}

function renderBidHistory(items) {
  if (!items || items.length === 0) {
    el.bidBody.innerHTML = '<tr><td colspan="7" class="empty-note">No bids yet.</td></tr>';
    return;
  }
  el.bidBody.innerHTML = items.map((b) => {
    const status = (b.status || "pending").toLowerCase();
    return `
      <tr>
        <td>#${esc(b.order_id)}</td>
        <td>${esc(b.lane || "—")}</td>
        <td>${esc(b.vehicle_type || "—")}</td>
        <td>${fmtMoney(b.bid_amount)}</td>
        <td>${fmtRate(b.rate_per_mile)}</td>
        <td><span class="status-pill status-${status}">${esc(b.status || "pending")}</span></td>
        <td>${fmtWhen(b.created_at)}</td>
      </tr>`;
  }).join("");
}

// ── Polling ────────────────────────────────────────────────────────
async function refreshAll() {
  try {
    const [stats, feed, hist] = await Promise.all([
      apiGet("/api/web/stats"),
      apiGet("/api/web/feed", { limit: 30 }),
      apiGet("/api/web/bid_history", { limit: 30 }),
    ]);
    renderStats(stats);
    renderFeed(feed.items);
    renderBidHistory(hist.items);
    setConn(true);
  } catch (err) {
    setConn(false);
    if (String(err.message).includes("License")) {
      // license got revoked/expired mid-session — bounce to login
      el.logoutBtn.click();
    }
  }
}

function setConn(ok) {
  el.connDot.classList.toggle("ok", ok);
  el.connDot.classList.toggle("bad", !ok);
}

function startPolling() {
  stopPolling();
  pollTimer = setInterval(refreshAll, POLL_MS);
}
function stopPolling() {
  if (pollTimer) clearInterval(pollTimer);
  pollTimer = null;
}

// ── Boot ───────────────────────────────────────────────────────────
initTheme();
try {
  const saved = localStorage.getItem(STORAGE_KEY);
  if (saved) {
    licenseKey = saved;
    el.loginKey.value = saved;
    showDashboard();
  }
} catch (e) {}
