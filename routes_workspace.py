#!/usr/bin/env python3
"""Workspace-scoped read surface: the dashboard's data API (GAP-1, 2026-09-13).

Until this router, `/v1/dashboard` and `/v1/agents` were operator-only
(X-Al-Admin) and a workspace owner had NO single page showing every agent's
spend — the product had charts nowhere and CSV nowhere. These routes give the
X-Workspace-Key holder exactly one workspace's data, nothing cross-tenant:

- GET /v1/workspace/summary   — every agent + totals + daily series + alerts
- GET /v1/workspace/export.csv — every entry as CSV
- GET /dashboard              — self-contained HTML page that renders the above

Security shape, deliberately the same as every other gate in the codebase:
authenticate FIRST (identity.resolve_workspace_key), then reveal — a bad key
is 401 before any existence information leaks. The HTML page itself carries
no data and needs no auth; the key is entered by the human in the browser and
held in sessionStorage (never in a URL, where it would leak into history,
Referer and server logs). All JS and CSS is inline — no CDN, no third-party
origin ever sees a workspace key or its data, and the CSP header pins that.
"""
import csv
import io
import json
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response

from ledger_engine import error_envelope, workspace_agents, window_stats, \
    agent_recent_alerts
import identity
import workspace_engine

router = APIRouter()

MAX_WINDOW_DAYS = 90


def _workspace_from_key_or_401(request: Request) -> tuple[str, dict]:
    """Same gate as routes_agents._workspace_key_or_401, but it also hands back
    the workspace record — the summary renders plan/cap from it. Runs before
    any data is touched: an invalid key must cost the caller nothing but the
    401."""
    raw = request.headers.get("x-workspace-key") or ""
    record = workspace_engine.get_workspace_by_key(raw) if raw else None
    if not record:
        raise HTTPException(401, detail=error_envelope(
            401, "this endpoint requires a valid X-Workspace-Key",
            code="workspace_key_required"))
    return record["workspace_id"], record


@router.get("/v1/workspace/summary")
def workspace_summary(request: Request, days: int = 30):
    """One workspace's whole cost picture: per-agent rows (spend, budget
    status, token burn, last activity, anomaly count), workspace totals,
    a zero-filled daily series for charts, and the merged alert feed."""
    days = max(1, min(days, MAX_WINDOW_DAYS))
    workspace_id, record = _workspace_from_key_or_401(request)

    agent_ids = workspace_agents(workspace_id)
    agents = []
    totals = {"spend_cents": 0, "by_rail": {}, "by_service": {},
              "tokens_in": 0, "tokens_out": 0, "entry_count": 0}
    daily = {}
    alerts = []
    for agent_id in agent_ids:
        stats = window_stats(agent_id, days)
        agents.append({
            "agent_id": agent_id,
            "spend_cents": stats["spend_cents"],
            "entry_count": stats["entry_count"],
            "tokens_in": stats["tokens_in"],
            "tokens_out": stats["tokens_out"],
            "budget": stats["budget_status"] or None,
            "last_event_ts": stats["last_event_ts"],
            "anomalies": stats["anomalies"],
            "by_service": stats["by_service"],
        })
        totals["spend_cents"] += stats["spend_cents"]
        totals["entry_count"] += stats["entry_count"]
        totals["tokens_in"] += stats["tokens_in"]
        totals["tokens_out"] += stats["tokens_out"]
        for k, v in stats["by_rail"].items():
            totals["by_rail"][k] = totals["by_rail"].get(k, 0) + v
        for k, v in stats["by_service"].items():
            totals["by_service"][k] = totals["by_service"].get(k, 0) + v
        for d, b in stats["daily"].items():
            cur = daily.setdefault(d, {"date": d, "spend_cents": 0,
                                       "tokens_in": 0, "tokens_out": 0})
            for f in ("spend_cents", "tokens_in", "tokens_out"):
                cur[f] += b[f]
        alerts.extend(agent_recent_alerts(agent_id, limit=5))

    alerts.sort(key=lambda a: str(a.get("timestamp", "")), reverse=True)

    cap = workspace_engine.effective_agent_cap(record)
    return {
        "workspace_id": workspace_id,
        "plan": "pro" if cap is None else "free",
        "agent_cap": cap,
        "agents_used": len(agent_ids),
        "days": days,
        "agents": agents,
        "totals": totals,
        "daily_series": [daily[k] for k in sorted(daily)],
        "alerts": alerts[:20],
    }


def _entries_csv(agent_id: str) -> list[dict]:
    """Every ledger row for one agent, oldest-first, as plain dicts."""
    from ledger_engine import _ledger_path, validate_agent_id, ValidationError
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, detail=error_envelope(422, str(e), code="invalid_agent_id"))
    path = _ledger_path(agent_id)
    rows = []
    if path.exists():
        for line in path.read_text().splitlines():
            try:
                rows.append(json.loads(line))
            except ValueError:
                continue
    return rows


CSV_COLUMNS = ["date", "timestamp", "agent_id", "rail", "service",
               "amount_cents", "tokens_in", "tokens_out", "model"]


def _csv_response(rows: list[dict]) -> Response:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(CSV_COLUMNS)
    for e in rows:
        w.writerow([str(e.get("timestamp", ""))[:10], e.get("timestamp", ""),
                    e.get("agent_id", ""), e.get("rail", ""), e.get("service", ""),
                    e.get("amount_cents", 0), e.get("tokens_in", ""),
                    e.get("tokens_out", ""), e.get("model", "")])
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition":
                 f'attachment; filename="agentledger-export-{datetime.now(timezone.utc).strftime("%Y%m%d")}.csv"'})


@router.get("/v1/workspace/export.csv")
def workspace_export(request: Request, days: int = 30):
    """Every entry of every agent in the workspace, one CSV. days=0 means all
    time — the reconciliation case where a windowed export is worse than
    none."""
    workspace_id, _ = _workspace_from_key_or_401(request)
    if days < 0:
        raise HTTPException(422, detail=error_envelope(
            422, "days must be >= 0 (0 = all time)", code="invalid_days"))
    days = min(days, 365) if days else None
    cutoff = None
    if days:
        cutoff = datetime.now(timezone.utc).timestamp() - days * 86400
    rows = []
    for agent_id in workspace_agents(workspace_id):
        for e in _entries_csv(agent_id):
            try:
                ts = datetime.fromisoformat(e["timestamp"]).timestamp()
            except (KeyError, ValueError):
                continue
            if cutoff is None or ts >= cutoff:
                rows.append(e)
    rows.sort(key=lambda e: str(e.get("timestamp", "")))
    return _csv_response(rows)


@router.get("/v1/report/{agent_id}/csv")
def report_csv(agent_id: str, request: Request, days: int = 0):
    """One agent's entries as CSV. Same read credential as the JSON report
    (agent_secret or workspace_key via identity.authorize_agent_access)."""
    from ledger_engine import agent_exists
    from ledger_engine import validate_agent_id, ValidationError
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, detail=error_envelope(422, str(e), code="invalid_agent_id"))
    ok = identity.authorize_agent_access(
        agent_id,
        agent_secret=request.headers.get("x-agent-secret"),
        workspace_key=request.headers.get("x-workspace-key"))
    if not ok:
        # authenticate-then-authorize: an unknown agent_id and a foreign
        # agent_id answer identically, so the 401 cannot be used to enumerate
        raise HTTPException(401, detail=error_envelope(
            401, "a valid X-Agent-Secret or X-Workspace-Key is required",
            code="unauthorized"))
    if not agent_exists(agent_id):
        raise HTTPException(404, detail=error_envelope(
            404, f"agent_id '{agent_id}' is not claimed", code="agent_not_claimed"))
    rows = _entries_csv(agent_id)
    if days < 0:
        raise HTTPException(422, detail=error_envelope(
            422, "days must be >= 0 (0 = all time)", code="invalid_days"))
    if days and days > 0:
        cutoff = datetime.now(timezone.utc).timestamp() - min(days, 365) * 86400
        rows = [e for e in rows
                if _parse_ts(e.get("timestamp", "")) >= cutoff]
    return _csv_response(rows)


def _parse_ts(ts: str) -> float:
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


DASHBOARD_CSP = ("default-src 'none'; script-src 'unsafe-inline'; "
                 "style-src 'unsafe-inline'; connect-src 'self'; "
                 "img-src 'self' data:; base-uri 'none'; form-action 'self'; "
                 "frame-ancestors 'none'")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard_page():
    """The human surface for the whole product: paste your workspace key,
    see every agent, every dollar, every token. The page carries no data and
    needs no auth — the browser holds the key in sessionStorage and calls the
    API directly. All JS/CSS is inline by design: no third-party origin is
    ever in a position to see a workspace key."""
    return HTMLResponse(_dashboard_html(), headers={
        "Content-Security-Policy": DASHBOARD_CSP,
        "X-Frame-Options": "DENY",
        "Cache-Control": "no-store",
    })


def _dashboard_html() -> str:
    return """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>AgentLedger Dashboard — Spending limits for AI agents</title>
<style>
  :root { color-scheme: dark; }
  * { box-sizing: border-box; margin: 0; }
  body { background: #0d1117; color: #e6edf3; font: 15px/1.5 -apple-system,
         BlinkMacSystemFont, "Segoe UI", Helvetica, Arial, sans-serif; }
  .wrap { max-width: 1080px; margin: 0 auto; padding: 28px 20px 80px; }
  header { display: flex; justify-content: space-between; align-items: baseline;
           flex-wrap: wrap; gap: 8px; margin-bottom: 6px; }
  h1 { font-size: 22px; letter-spacing: -0.02em; }
  h1 span { color: #d29922; }
  .sub { color: #8b949e; font-size: 13px; margin-bottom: 22px; }
  a { color: #58a6ff; text-decoration: none; }
  .card { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
          padding: 18px; margin-bottom: 16px; }
  .kpis { display: grid; grid-template-columns: repeat(auto-fit, minmax(170px, 1fr));
          gap: 12px; margin-bottom: 16px; }
  .kpi { background: #161b22; border: 1px solid #30363d; border-radius: 10px;
         padding: 14px 16px; }
  .kpi .label { color: #8b949e; font-size: 12px; text-transform: uppercase;
                letter-spacing: 0.06em; }
  .kpi .val { font-size: 24px; font-weight: 600; margin-top: 2px; }
  .kpi .note { color: #8b949e; font-size: 12px; }
  table { width: 100%; border-collapse: collapse; font-size: 14px; }
  th { color: #8b949e; text-align: left; font-weight: 500; font-size: 12px;
       text-transform: uppercase; letter-spacing: 0.05em; padding: 8px 10px;
       border-bottom: 1px solid #30363d; }
  td { padding: 9px 10px; border-bottom: 1px solid #21262d; }
  tr:last-child td { border-bottom: none; }
  .num { text-align: right; font-variant-numeric: tabular-nums; }
  .bar { background: #21262d; border-radius: 4px; height: 7px; width: 110px;
         overflow: hidden; display: inline-block; vertical-align: middle; }
  .bar i { display: block; height: 100%; background: #3fb950; }
  .bar i.warn { background: #d29922; }
  .bar i.over { background: #f85149; }
  .badge { font-size: 11px; padding: 1px 8px; border-radius: 10px;
           border: 1px solid #30363d; color: #8b949e; }
  .badge.pro { color: #d29922; border-color: #d29922; }
  button { background: #21262d; color: #e6edf3; border: 1px solid #30363d;
           border-radius: 6px; padding: 6px 12px; font-size: 13px; cursor: pointer; }
  button:hover { border-color: #8b949e; }
  button.primary { background: #d29922; border-color: #d29922; color: #0d1117;
                   font-weight: 600; }
  input[type=password] { background: #0d1117; color: #e6edf3;
         border: 1px solid #30363d; border-radius: 6px; padding: 8px 10px;
         width: 340px; max-width: 100%; font-size: 13px; }
  .gate { max-width: 560px; margin: 60px auto; text-align: left; }
  .gate .card { padding: 28px; }
  .err { color: #f85149; font-size: 13px; min-height: 20px; margin-top: 10px; }
  .muted { color: #8b949e; font-size: 12px; }
  .actions { display: flex; gap: 8px; justify-content: space-between;
             flex-wrap: wrap; align-items: center; margin-bottom: 14px; }
  h2 { font-size: 15px; margin-bottom: 10px; }
  .chart { width: 100%; height: 160px; display: block; }
  .alert-row { display: flex; gap: 10px; padding: 7px 0;
               border-bottom: 1px solid #21262d; font-size: 13px; }
  .alert-row:last-child { border-bottom: none; }
  .alert-row .t { color: #d29922; white-space: nowrap; font-size: 12px; }
  .alert-row .a { color: #8b949e; white-space: nowrap; }
  .flex2 { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }
  @media (max-width: 800px) { .flex2 { grid-template-columns: 1fr; } }
  svg text { fill: #8b949e; font-size: 10px; }
  .pill { font-size: 12px; color: #8b949e; }
</style>
</head>
<body>
<div class="wrap">
  <header>
    <h1>Agent<span>Ledger</span> Dashboard</h1>
    <div id="plan" class="pill"></div>
  </header>
  <div class="sub">Every agent, every dollar, every token — one workspace.
    <a href="/">docs</a> · <a href="/status">status</a></div>

  <div id="gate" class="gate">
    <div class="card">
      <h2>Your workspace key</h2>
      <p class="muted" style="margin-bottom:14px">Paste the <b>wk_live_…</b> key
        from your <a href="/start">/start</a> page. It stays in this browser's
        session storage — never in a URL, never sent anywhere but this site.</p>
      <form id="keyform">
        <input type="password" id="wk" placeholder="wk_live_…" autocomplete="off">
        <button class="primary" type="submit">Open dashboard</button>
      </form>
      <label class="muted" style="display:block;margin-top:10px">
        <input type="checkbox" id="remember"> remember on this device (localStorage)
      </label>
      <div class="err" id="gateerr"></div>
    </div>
  </div>

  <div id="app" style="display:none">
    <div class="actions">
      <div><span class="muted" id="wsid"></span></div>
      <div>
        <button id="refresh">Refresh</button>
        <button id="csv">Export CSV</button>
        <button id="logout">Close session</button>
      </div>
    </div>

    <div class="kpis">
      <div class="kpi"><div class="label">Spend (30d)</div><div class="val" id="k-spend">$0.00</div><div class="note" id="k-spend-note"></div></div>
      <div class="kpi"><div class="label">Tokens (30d)</div><div class="val" id="k-tokens">0</div><div class="note" id="k-tokens-note"></div></div>
      <div class="kpi"><div class="label">Agents</div><div class="val" id="k-agents">0</div><div class="note" id="k-agents-note"></div></div>
      <div class="kpi"><div class="label">Alerts (latest)</div><div class="val" id="k-alerts">0</div><div class="note">across workspace</div></div>
    </div>

    <div class="card">
      <h2>Daily spend — last 30 days</h2>
      <svg id="chart" class="chart" viewBox="0 0 1000 160" preserveAspectRatio="none"></svg>
    </div>

    <div class="card">
      <h2>Agents</h2>
      <table id="agents">
        <thead><tr><th>Agent</th><th class="num">Spend 30d</th><th class="num">Tokens 30d</th>
          <th>Budget (month)</th><th>Last activity</th><th></th></tr></thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="flex2">
      <div class="card">
        <h2>By rail</h2>
        <table id="byrail"><thead><tr><th>Rail</th><th class="num">Spend 30d</th></tr></thead><tbody></tbody></table>
      </div>
      <div class="card">
        <h2>Alerts</h2>
        <div id="alerts"></div>
      </div>
    </div>
  </div>
</div>
<script>
"use strict";
var WK_KEY = "al_wk_session", WK_REMEMBER = "al_wk_remember";
function money(c) { return "$" + (c / 100).toLocaleString(undefined, {minimumFractionDigits: 2, maximumFractionDigits: 2}); }
function tokfmt(n) { return n >= 1000000 ? (n / 1000000).toFixed(1) + "M" : n >= 1000 ? (n / 1000).toFixed(1) + "k" : String(n); }
function when(ts) {
  if (!ts) return "never";
  var d = new Date(ts), s = (Date.now() - d.getTime()) / 1000;
  if (s < 90) return "just now";
  if (s < 3600) return Math.floor(s / 60) + "m ago";
  if (s < 86400) return Math.floor(s / 3600) + "h ago";
  return Math.floor(s / 86400) + "d ago";
}
function getWK() {
  try { return sessionStorage.getItem(WK_KEY) || localStorage.getItem(WK_KEY) || ""; }
  catch (e) { return ""; }
}
function setWK(k, remember) {
  try {
    sessionStorage.setItem(WK_KEY, k);
    if (remember) localStorage.setItem(WK_KEY, k); else localStorage.removeItem(WK_KEY);
  } catch (e) {}
}
function clearWK() {
  try { sessionStorage.removeItem(WK_KEY); localStorage.removeItem(WK_KEY); } catch (e) {}
}
function esc(s) { return String(s); }  // values go through textContent only

function api(path, opts) {
  var wk = getWK();
  if (!wk) { showGate("no key in this session"); return Promise.reject(); }
  var h = { "X-Workspace-Key": wk };
  if (opts && opts.method && opts.method !== "GET") h["Content-Type"] = "application/json";
  return fetch(path, { headers: h, method: opts && opts.method || "GET" })
    .then(function (r) {
      if (r.status === 401) { showGate("That key was not accepted. Check it and try again."); throw new Error("401"); }
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.json();
    });
}

function showGate(msg) {
  document.getElementById("app").style.display = "none";
  document.getElementById("gate").style.display = "block";
  if (msg) document.getElementById("gateerr").textContent = msg;
}
function showApp() {
  document.getElementById("gate").style.display = "none";
  document.getElementById("app").style.display = "block";
  document.getElementById("gateerr").textContent = "";
}

function drawChart(series) {
  var svg = document.getElementById("chart");
  while (svg.firstChild) svg.removeChild(svg.firstChild);
  var W = 1000, H = 160, pad = 4, bw = W / Math.max(series.length, 1);
  var max = 1;
  for (var i = 0; i < series.length; i++) max = Math.max(max, series[i].spend_cents);
  var ns = "http://www.w3.org/2000/svg";
  for (var j = 0; j < series.length; j++) {
    var b = series[j], h = Math.max(b.spend_cents > 0 ? 3 : 1, (b.spend_cents / max) * (H - 34));
    var r = document.createElementNS(ns, "rect");
    r.setAttribute("x", pad + j * bw + 1);
    r.setAttribute("y", H - 22 - h);
    r.setAttribute("width", Math.max(bw - 2, 1));
    r.setAttribute("height", h);
    r.setAttribute("fill", b.spend_cents > 0 ? "#d29922" : "#21262d");
    var t = document.createElementNS(ns, "title");
    t.textContent = b.date + " — " + money(b.spend_cents) +
      " · " + tokfmt(b.tokens_in + b.tokens_out) + " tokens";
    r.appendChild(t);
    svg.appendChild(r);
  }
  var lbl = document.createElementNS(ns, "text");
  lbl.setAttribute("x", 2); lbl.setAttribute("y", 12);
  lbl.textContent = "peak " + money(max) + "/day";
  svg.appendChild(lbl);
  var first = document.createElementNS(ns, "text");
  first.setAttribute("x", 2); first.setAttribute("y", H - 6);
  first.textContent = series.length ? series[0].date : "";
  svg.appendChild(first);
  var last = document.createElementNS(ns, "text");
  last.setAttribute("x", W - 84); last.setAttribute("y", H - 6);
  last.textContent = series.length ? series[series.length - 1].date : "";
  svg.appendChild(last);
}

function render(s) {
  showApp();
  var planEl = document.getElementById("plan");
  planEl.textContent = "";
  var b = document.createElement("span");
  b.className = "badge" + (s.plan === "pro" ? " pro" : "");
  b.textContent = s.plan === "pro" ? "PRO workspace" : "free workspace";
  planEl.appendChild(b);
  document.getElementById("wsid").textContent =
    s.workspace_id + " · " + s.agents_used + (s.agent_cap ? "/" + s.agent_cap : "") + " agents used";

  document.getElementById("k-spend").textContent = money(s.totals.spend_cents);
  document.getElementById("k-spend-note").textContent = s.totals.entry_count + " entries";
  var t = s.totals.tokens_in + s.totals.tokens_out;
  document.getElementById("k-tokens").textContent = tokfmt(t);
  document.getElementById("k-tokens-note").textContent =
    tokfmt(s.totals.tokens_in) + " in / " + tokfmt(s.totals.tokens_out) + " out";
  document.getElementById("k-agents").textContent = s.agents_used;
  document.getElementById("k-agents-note").textContent =
    s.agent_cap ? "cap " + s.agent_cap : "unlimited";
  document.getElementById("k-alerts").textContent = s.alerts.length;

  drawChart(s.daily_series);

  var tb = document.querySelector("#agents tbody");
  tb.textContent = "";
  s.agents.forEach(function (a) {
    var tr = document.createElement("tr");

    var td = document.createElement("td");
    var strong = document.createElement("b");
    strong.textContent = a.agent_id;
    td.appendChild(strong);
    if (a.anomalies > 0) {
      var w = document.createElement("span");
      w.className = "badge"; w.style.color = "#d29922"; w.style.marginLeft = "6px";
      w.textContent = a.anomalies + " spike" + (a.anomalies > 1 ? "s" : "");
      td.appendChild(w);
    }
    tr.appendChild(td);

    td = document.createElement("td"); td.className = "num";
    td.textContent = money(a.spend_cents);
    tr.appendChild(td);

    td = document.createElement("td"); td.className = "num";
    td.textContent = tokfmt(a.tokens_in + a.tokens_out);
    tr.appendChild(td);

    td = document.createElement("td");
    if (a.budget && a.budget.monthly_cap_cents) {
      var bar = document.createElement("span"); bar.className = "bar";
      var fill = document.createElement("i");
      var pct = Math.min(a.budget.pct_used, 100);
      fill.style.width = pct + "%";
      fill.className = a.budget.exceeded ? "over" : (pct >= 80 ? "warn" : "");
      bar.appendChild(fill);
      td.appendChild(bar);
      var lab = document.createElement("span");
      lab.className = "muted"; lab.style.marginLeft = "8px";
      lab.textContent = a.budget.pct_used + "% of " + money(a.budget.monthly_cap_cents);
      td.appendChild(lab);
    } else {
      td.textContent = "—";
    }
    tr.appendChild(td);

    td = document.createElement("td"); td.className = "muted";
    td.textContent = when(a.last_event_ts);
    tr.appendChild(td);

    td = document.createElement("td");
    var btn = document.createElement("button");
    btn.textContent = "Share";
    btn.onclick = function () { share(a.agent_id, btn); };
    td.appendChild(btn);
    tr.appendChild(td);

    tb.appendChild(tr);
  });

  var rb = document.querySelector("#byrail tbody");
  rb.textContent = "";
  Object.keys(s.totals.by_rail).sort().forEach(function (rail) {
    var tr = document.createElement("tr");
    var d1 = document.createElement("td"); d1.textContent = rail;
    var d2 = document.createElement("td"); d2.className = "num";
    d2.textContent = money(s.totals.by_rail[rail]);
    tr.appendChild(d1); tr.appendChild(d2); rb.appendChild(tr);
  });
  if (!rb.children.length) {
    var tr2 = document.createElement("tr");
    var d = document.createElement("td"); d.colSpan = 2; d.className = "muted";
    d.textContent = "no spend recorded yet";
    tr2.appendChild(d); rb.appendChild(tr2);
  }

  var al = document.getElementById("alerts");
  al.textContent = "";
  if (!s.alerts.length) {
    var p = document.createElement("p"); p.className = "muted";
    p.textContent = "No alerts. Budgets are quiet.";
    al.appendChild(p);
  }
  s.alerts.forEach(function (a) {
    var row = document.createElement("div"); row.className = "alert-row";
    var t1 = document.createElement("span"); t1.className = "t";
    t1.textContent = String(a.type || "alert");
    var t2 = document.createElement("span"); t2.className = "a";
    t2.textContent = a.agent_id;
    var t3 = document.createElement("span");
    t3.textContent = a.message || "";
    row.appendChild(t1); row.appendChild(t2); row.appendChild(t3);
    al.appendChild(row);
  });
}

function share(agentId, btn) {
  btn.disabled = true; btn.textContent = "…";
  api("/v1/report/" + encodeURIComponent(agentId) + "/share", { method: "POST" })
    .then(function (r) {
      if (r && r.url) {
        window.prompt("Read-only share link (valid " + (r.ttl_days || 7) + " days):", r.url);
      }
      btn.disabled = false; btn.textContent = "Share";
    })
    .catch(function () { btn.disabled = false; btn.textContent = "Share"; });
}

function load() {
  api("/v1/workspace/summary?days=30")
    .then(render)
    .catch(function (e) { if (e && e.message !== "401") showGate("Could not load summary: " + e.message); });
}

document.getElementById("keyform").onsubmit = function (ev) {
  ev.preventDefault();
  var k = document.getElementById("wk").value.trim();
  var remember = document.getElementById("remember").checked;
  if (!k) return;
  setWK(k, remember);
  load();
};
document.getElementById("refresh").onclick = load;
document.getElementById("logout").onclick = function () { clearWK(); showGate(""); };
document.getElementById("csv").onclick = function () {
  var wk = getWK();
  if (!wk) { showGate(""); return; }
  fetch("/v1/workspace/export.csv?days=0", { headers: { "X-Workspace-Key": wk } })
    .then(function (r) {
      if (!r.ok) throw new Error("HTTP " + r.status);
      return r.blob();
    })
    .then(function (blob) {
      var a = document.createElement("a");
      a.href = URL.createObjectURL(blob);
      a.download = "agentledger-export.csv";
      document.body.appendChild(a); a.click(); a.remove();
    })
    .catch(function (e) { alert("Export failed: " + e.message); });
};

if (getWK()) load();
</script>
</body>
</html>
"""
