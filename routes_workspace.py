#!/usr/bin/env python3
"""The workspace-facing surfaces: a summary API, two CSV exports, and the
dashboard (D-1226, GAP-1/GAP-2 of the v2 gap-closure plan).

Everything here is authenticated by the WORKSPACE key and is read-only. The
product had no surface that showed a user all of their agents at once — the
audit's finding was blunt: "the UI is curl". This is that surface.

Three deliberate choices, each because the alternative was a credential risk:

1. The workspace key is sent as a HEADER on every fetch and lives in
   sessionStorage. It never appears in a URL, a query string, a page title, a
   referrer, or a server log. A key in a URL leaks into history, into the
   Referer header of every subsequent request, and into any proxy in between.
2. The CSP allows NO external resource at all (default-src 'none'). A page
   holding a workspace key must not be able to load a third-party script, so
   the chart is inline SVG rendered from the JSON rather than a chart library.
   Vendoring a library would have meant shipping and updating one; forbidding
   external loads is a header, and it cannot be undone by a later edit.
3. CSV is generated server-side from the same ledger reader the report uses.
"""
import csv
import io
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, Response
from pydantic import BaseModel

import ledger_engine as engine
from routes_agents import _workspace_key_or_401

router = APIRouter()

CSV_COLUMNS = ["timestamp", "agent_id", "rail", "service", "amount_cents",
               "tokens_in", "tokens_out", "model"]

# No external origins, ever. script/style are inline because the page is
# self-contained on purpose; everything else is 'none'.
CSP = ("default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; base-uri 'none'; "
       "form-action 'none'; frame-ancestors 'none'")


def _csv_response(rows: list, filename: str) -> Response:
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=CSV_COLUMNS, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow(row)
    return Response(
        content=buf.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"',
                 "X-Content-Type-Options": "nosniff"},
    )


@router.get("/v1/workspace/summary")
def workspace_summary(request: Request, days: int = 30):
    """Every agent in the workspace with 30-day spend, budget status, the daily
    series, and the alert feed. Workspace key only."""
    if not 1 <= days <= 365:
        raise HTTPException(422, detail=engine.error_envelope(
            422, "days must be between 1 and 365", code="invalid_window"))
    workspace_id = _workspace_key_or_401(request, None,
                                         purpose="reading the workspace summary")
    return engine.workspace_summary(workspace_id, days)


@router.get("/v1/workspace/export.csv")
def workspace_export(request: Request, days: int = 30):
    workspace_id = _workspace_key_or_401(request, None,
                                         purpose="exporting the workspace ledger")
    rows = engine.export_rows(engine.workspace_agents(workspace_id), days)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _csv_response(rows, f"agentledger-{workspace_id}-{stamp}.csv")


@router.get("/v1/report/{agent_id}/csv")
def report_export(agent_id: str, request: Request, days: int = 30):
    """Per-agent CSV. Two credentials can authorize it, exactly like the JSON
    report: the agent's own secret, or the workspace key that owns it."""
    import identity
    from ledger_engine import ValidationError, validate_agent_id
    try:
        validate_agent_id(agent_id)
    except ValidationError as e:
        raise HTTPException(422, detail=engine.error_envelope(422, str(e),
                                                             code="invalid_agent_id"))
    ok = identity.authorize_agent_access(
        agent_id,
        agent_secret=request.headers.get("x-agent-secret"),
        workspace_key=request.headers.get("x-workspace-key"))
    if not ok:
        raise HTTPException(401, detail=engine.error_envelope(
            401, "reading this agent's ledger requires its agent_secret or the "
                 "workspace_key that owns it", code="unauthorized"))
    rows = engine.export_rows([agent_id], days)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d")
    return _csv_response(rows, f"agentledger-{agent_id}-{stamp}.csv")


# ── the dashboard ──────────────────────────────────────────────────────────

_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AgentLedger — workspace dashboard</title>
<meta name="referrer" content="no-referrer">
<style>
:root{color-scheme:dark}
body{background:#0d1117;color:#e6edf3;font-family:-apple-system,BlinkMacSystemFont,sans-serif;
     margin:0;padding:24px;max-width:1040px;margin:0 auto}
h1{font-size:20px;margin:0 0 2px} h2{font-size:14px;color:#8b949e;margin:28px 0 8px;
     text-transform:uppercase;letter-spacing:.04em}
.sub{color:#8b949e;font-size:12px;margin-bottom:18px}
.card{background:#161b22;border:1px solid #30363d;border-radius:8px;padding:14px 16px}
.kpis{display:flex;gap:12px;flex-wrap:wrap;margin:16px 0}
.kpi{flex:1 1 160px}.kpi .k{color:#8b949e;font-size:11px;text-transform:uppercase}
.kpi .v{font-size:22px;font-weight:600;margin-top:2px}
table{border-collapse:collapse;width:100%;font-size:13px}
th,td{text-align:left;padding:7px 10px;border-bottom:1px solid #30363d}
th{color:#8b949e;font-weight:600;font-size:11px;text-transform:uppercase}
td.num{text-align:right;font-variant-numeric:tabular-nums}
.bar{background:#21262d;border-radius:6px;height:8px;width:100px;display:inline-block;
     vertical-align:middle}
.bar > i{display:block;height:8px;border-radius:6px}
.ok > i{background:#3fb950}.warning > i{background:#d29922}.exceeded > i{background:#f85149}
.pill{display:inline-block;border-radius:10px;padding:1px 8px;font-size:11px;font-weight:600}
.pill.ok{background:#0f2f1a;color:#3fb950}.pill.warning{background:#3a2d0a;color:#d29922}
.pill.exceeded{background:#3d1418;color:#f85149}
a{color:#58a6ff} button{background:#238636;border:0;color:#fff;border-radius:6px;
padding:8px 14px;font-size:13px;font-weight:600;cursor:pointer}
button.ghost{background:#21262d;color:#e6edf3}
input{background:#0d1117;border:1px solid #30363d;color:#e6edf3;border-radius:6px;
padding:9px 11px;font-size:13px;width:100%;max-width:460px;font-family:ui-monospace,monospace}
.muted{color:#8b949e;font-size:12px}
.alerts li{font-size:13px;margin-bottom:4px}
code{background:#21262d;padding:1px 5px;border-radius:4px;font-size:12px}
</style></head><body>
<h1>AgentLedger <span class="pill ok" style="background:#0f2f1a;color:#3fb950">workspace</span></h1>
<div class="sub">An AI Agent City product</div>

<div id="gate">
  <div class="card" style="max-width:560px">
    <p style="margin-top:0"><b>Paste your workspace key</b></p>
    <p class="muted">It starts <code>wk_live_</code>. It is kept in this browser
    tab's session storage and sent only as a request header to this site.</p>
    <p><input id="keyin" type="password" autocomplete="off" spellcheck="false"
              placeholder="wk_live_..."></p>
    <p><label class="muted"><input type="checkbox" id="remember"
       style="width:auto;vertical-align:middle"> remember on this device</label></p>
    <p><button id="go">Open dashboard</button></p>
    <p id="gateerr" class="muted" style="color:#f85149"></p>
  </div>
</div>

<div id="app" style="display:none">
  <div class="kpis" id="kpis"></div>
  <div class="card" id="planbox">
    <p style="margin-top:0"><b>Plan: <span id="plantier">free</span></b>
       <span class="muted">— free tier is 3 agents per workspace.</span></p>
    <p class="muted" id="planhint" style="margin-bottom:8px">Need more? $19/mo removes the
       agent limit on this workspace. No migration, same key.</p>
    <p><button id="upgrade">Upgrade to Pro — $19/mo</button>
       <span class="muted" id="upmsg"></span></p>
  </div>
  <p><button class="ghost" id="csv">Download CSV</button>
     <button class="ghost" id="forget">Forget key</button></p>
  <h2>Daily spend (30 days)</h2>
  <div class="card" id="chart"></div>
  <h2>Agents</h2>
  <div class="card"><table id="agents"></table></div>
  <h2>Breakdown</h2>
  <div class="card"><table id="breakdown"></table></div>
  <h2>Alerts</h2>
  <div class="card"><ul class="alerts" id="alerts"></ul></div>
  <h2>Webhooks</h2>
  <div class="card">
    <p class="muted" style="margin-top:0">Alerts are pushed to a URL you own —
    Slack, Discord, Zapier or your own endpoint — with retries. Every attempt is
    logged below, successes and failures alike.</p>
    <p><input id="whurl" type="url" placeholder="your-endpoint-url (Slack, Discord, Zapier)"
              spellcheck="false"></p>
    <p id="whev"></p>
    <p><input id="whlabel" type="text" placeholder="label (optional, e.g. #alerts)"></p>
    <p><button id="whadd">Register webhook</button></p>
    <p id="whmsg" class="muted"></p>
    <table id="whlist"></table>
    <p><a href="#" id="whlog-toggle" class="muted">show recent deliveries</a></p>
    <table id="whlog" style="display:none"></table>
  </div>
</div>

<script>
var KEY = null;
function store(k, remember){
  try { (remember ? localStorage : sessionStorage).setItem('al_key', k); } catch(e){}
}
function recall(){
  try { return sessionStorage.getItem('al_key') || localStorage.getItem('al_key'); }
  catch(e){ return null; }
}
function forget(){
  try { sessionStorage.removeItem('al_key'); localStorage.removeItem('al_key'); } catch(e){}
  location.reload();
}
function esc(s){ return String(s).replace(/[&<>"']/g, function(c){
  return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]; }); }
function money(c){ return '$' + (c/100).toFixed(2); }
function kfmt(n){ return n>=1e6 ? (n/1e6).toFixed(2)+'M' : n>=1e3 ? (n/1e3).toFixed(1)+'k' : String(n); }

function svgChart(series){
  if(!series.length) return '<p class="muted">No spend in this window.</p>';
  var W=960,H=150,pad=24,max=Math.max.apply(null, series.map(function(p){return p.spend_cents;}))||1;
  var bw = Math.max(2, (W-pad*2)/series.length - 2);
  var bars = series.map(function(p,i){
    var h = Math.round((p.spend_cents/max)*(H-pad*2));
    var x = pad + i*((W-pad*2)/series.length);
    var y = H-pad-h;
    return '<rect x="'+x.toFixed(1)+'" y="'+y+'" width="'+bw.toFixed(1)+'" height="'+h+
           '" fill="#58a6ff" rx="2"><title>'+esc(p.date)+': '+money(p.spend_cents)+'</title></rect>';
  }).join('');
  var first=series[0].date, last=series[series.length-1].date;
  return '<svg viewBox="0 0 '+W+' '+H+'" width="100%" height="'+H+'" role="img" '+
    'aria-label="Daily spend, '+esc(first)+' to '+esc(last)+', peak '+money(max)+'">'+bars+'</svg>'+
    '<div class="muted">'+esc(first)+' → '+esc(last)+' · peak '+money(max)+'</div>';
}

function render(d){
  document.getElementById('gate').style.display='none';
  document.getElementById('app').style.display='block';
  var t=d.totals||{}, agents=d.agents||[];
  document.getElementById('kpis').innerHTML =
    kpi('30-day spend', money(t.spend_cents_30d||0)) +
    kpi('30-day tokens', kfmt(t.tokens_in_30d||0)+' in / '+kfmt(t.tokens_out_30d||0)+' out') +
    kpi('agents tracked', String(agents.length)) +
    kpi('alerts', String((d.alerts||[]).length));
  document.getElementById('chart').innerHTML = svgChart(d.daily_series||[]);
  // Event checkboxes: all ticked by default, matching the API's own default
  // (omit `events` to receive every one).
  document.getElementById('whev').innerHTML = WH_EVENTS.map(function(e){
    return '<label class="muted" style="margin-right:14px">' +
      '<input type="checkbox" id="ev_'+e.replace(/\./g,'_')+'" checked ' +
      'style="width:auto;vertical-align:middle"> '+esc(e)+'</label>';
  }).join('');
  loadWebhooks();
  var rows = agents.map(function(a){
    var b=a.budget||{}, pct=b.used_pct||0, st=b.status||'ok';
    return '<tr><td>'+esc(a.agent_id)+'</td><td>'+esc(a.tier)+'</td>'+
      '<td class="num">'+money(a.spend_cents_30d)+'</td>'+
      '<td><span class="bar '+st+'"><i style="width:'+Math.min(100,pct)+'%"></i></span> '+
      (b.monthly_cents? pct+'%':'no cap')+'</td>'+
      '<td><span class="pill '+st+'">'+esc(st)+'</span></td>'+
      '<td>'+(a.last_event_ts?esc(String(a.last_event_ts).slice(0,16)):'—')+'</td>'+
      '<td><a href="/v1/report/'+encodeURIComponent(a.agent_id)+'/html?t=" target="_blank" '+
      'rel="noreferrer">report api</a></td></tr>';
  }).join('');
  document.getElementById('agents').innerHTML =
    '<tr><th>agent</th><th>plan</th><th>30d spend</th><th>budget</th><th>status</th>'+
    '<th>last event</th><th></th></tr>' + (rows || '<tr><td colspan="7" class="muted">'+
    'No agents in this workspace yet.</td></tr>');
  function tbl(title, obj){
    var ks = Object.keys(obj||{});
    return '<tr><th>'+title+'</th><th>spend</th></tr>' + (ks.length? ks.map(function(k){
      return '<tr><td>'+esc(k)+'</td><td class="num">'+money(obj[k])+'</td></tr>'; }).join('')
      : '<tr><td colspan="2" class="muted">none</td></tr>');
  }
  document.getElementById('breakdown').innerHTML =
    tbl('service', t.by_service) + '<tr><td colspan="2" style="height:10px"></td></tr>' +
    tbl('rail', t.by_rail);
  var al=(d.alerts||[]).map(function(a){
    return '<li><span class="muted">'+esc(String(a.timestamp||'').slice(0,16))+'</span> '+
      '<b>'+esc(a.agent_id||'')+'</b> '+esc(a.type||'')+' — '+esc(a.message||'')+'</li>';
  }).join('');
  document.getElementById('alerts').innerHTML = al || '<li class="muted">No alerts.</li>';
}
function kpi(k,v){ return '<div class="card kpi"><div class="k">'+esc(k)+
  '</div><div class="v">'+esc(v)+'</div></div>'; }

function load(key){
  fetch('/v1/workspace/summary?days=30', {headers:{'X-Workspace-Key':key}})
    .then(function(r){ if(!r.ok) throw new Error(r.status===401 ? 'That key was not accepted.' : 'HTTP '+r.status);
      return r.json(); })
    .then(function(d){
      if(!d.workspace_id) return;
      KEY=key; render(d);
    })
    .catch(function(e){
      document.getElementById('gate').style.display='block';
      document.getElementById('app').style.display='none';
      document.getElementById('gateerr').textContent = e.message;
    });
}
document.getElementById('go').onclick = function(){
  var k=document.getElementById('keyin').value.trim();
  if(!k) return;
  store(k, document.getElementById('remember').checked);
  load(k);
};
document.getElementById('keyin').addEventListener('keydown', function(e){
  if(e.key==='Enter') document.getElementById('go').click();
});
document.getElementById('forget').onclick = forget;
document.getElementById('csv').onclick = function(){
  if(!KEY) return;
  fetch('/v1/workspace/export.csv?days=30', {headers:{'X-Workspace-Key':KEY}})
    .then(function(r){ return r.blob(); })
    .then(function(b){
      var u=URL.createObjectURL(b), a=document.createElement('a');
      a.href=u; a.download='agentledger-export.csv'; document.body.appendChild(a);
      a.click(); a.remove(); URL.revokeObjectURL(u);
    });
};
var existing = recall();
if (existing) load(existing);

// ── the pay door ───────────────────────────────────────────────────────────
// WHY THIS EXISTS (2026-09-18, D-1382). The checkout endpoint worked and the
// success screen linked to it — but that screen is shown ONCE, at mint, labelled
// "Optional, and not needed today." The thing that triggers a purchase is the
// 3-agent cap, and that happens LATER, after integration, on THIS page. At that
// moment there was no upgrade control here and /upgrade and /pricing were both
// 404, so the customer who had just hit the exact limit we charge for had no
// door to walk through. Same defect class as the webhooks surface below: the
// endpoint existed and no page linked to it.
document.getElementById('upgrade').onclick = function(){
  if(!KEY) return;
  var btn = this, msg = document.getElementById('upmsg');
  btn.disabled = true; msg.textContent = 'Creating your checkout…';
  msg.style.color = '#8b949e';
  fetch('/v1/billing/checkout', {
    method:'POST',
    headers:{'X-Workspace-Key':KEY, 'X-Workspace-Id':WSID || '', 'Content-Type':'application/json'},
    body:'{}'
  })
    .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, body:j}; }); })
    .then(function(res){
      var url = res.body && res.body.checkout_url;
      if (url) { msg.textContent = 'Opening secure checkout…'; location.href = url; return; }
      btn.disabled = false;
      msg.textContent = (res.body && res.body.error && res.body.error.message) ||
                        'Could not start checkout — email entradox@icloud.com and we will sort it.';
      msg.style.color = '#f85149';
    })
    .catch(function(){
      btn.disabled = false;
      msg.textContent = 'Network error — try again.';
      msg.style.color = '#f85149';
    });
};

// ── webhooks ───────────────────────────────────────────────────────────────
// The endpoint existed and the marketing copy promised pushed alerts, but no
// page linked to it: the only way to register a destination was to hand-craft
// an API call from the docs. This is the missing surface.
var WH_EVENTS = ['alert.raised','budget.warning','budget.exceeded','anomaly.detected'];
function whMsg(text, bad){
  var el = document.getElementById('whmsg');
  el.textContent = text || '';
  el.style.color = bad ? '#f85149' : '#3fb950';
}
function whApi(path, opts){
  opts = opts || {};
  opts.headers = Object.assign({'X-Workspace-Key': KEY}, opts.headers || {});
  return fetch(path, opts);
}
function renderWebhooks(list){
  var rows = (list || []).map(function(w){
    return '<tr><td><code>'+esc(w.url)+'</code></td>'+
      '<td>'+esc((w.events||[]).join(', '))+'</td>'+
      '<td>'+esc(w.label||'—')+'</td>'+
      '<td><a href="#" class="whdel" data-id="'+esc(w.id)+'">remove</a></td></tr>';
  }).join('');
  document.getElementById('whlist').innerHTML =
    '<tr><th>url</th><th>events</th><th>label</th><th></th></tr>' +
    (rows || '<tr><td colspan="4" class="muted">No webhooks yet.</td></tr>');
  Array.prototype.forEach.call(document.querySelectorAll('.whdel'), function(a){
    a.onclick = function(e){
      e.preventDefault();
      whApi('/v1/webhooks/'+encodeURIComponent(a.getAttribute('data-id')), {method:'DELETE'})
        .then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
        .then(function(res){
          whMsg(res.ok ? 'Removed.' : (res.d.error ? res.d.error.message : 'Failed'), !res.ok);
          return loadWebhooks();
        });
    };
  });
}
function loadWebhooks(){
  if(!KEY) return;
  whApi('/v1/webhooks').then(function(r){ return r.json(); }).then(function(d){
    renderWebhooks(d.webhooks);
  }).catch(function(){});
}
function loadDeliveries(){
  whApi('/v1/webhooks/deliveries?limit=50').then(function(r){ return r.json(); })
    .then(function(d){
      var rows = (d.deliveries||[]).map(function(i){
        var ok = i.status === 'delivered';
        return '<tr><td>'+esc(String(i.timestamp||'').slice(0,19))+'</td>'+
          '<td>'+esc(i.event||'')+'</td>'+
          '<td><span class="pill '+(ok?'ok':'exceeded')+'">'+esc(i.status||'')+'</span></td>'+
          '<td>'+esc(String(i.error||i.url||'').slice(0,60))+'</td></tr>';
      }).join('');
      document.getElementById('whlog').innerHTML =
        '<tr><th>when</th><th>event</th><th>status</th><th>detail</th></tr>' +
        (rows || '<tr><td colspan="4" class="muted">No deliveries recorded yet.</td></tr>');
    }).catch(function(){});
}
document.getElementById('whlog-toggle').onclick = function(e){
  e.preventDefault();
  var t = document.getElementById('whlog');
  var show = t.style.display === 'none';
  t.style.display = show ? 'table' : 'none';
  this.textContent = show ? 'hide recent deliveries' : 'show recent deliveries';
  if (show) loadDeliveries();
};
document.getElementById('whadd').onclick = function(){
  if(!KEY) return;
  var url = document.getElementById('whurl').value.trim();
  if(!url){ whMsg('Enter a URL first.', true); return; }
  var events = WH_EVENTS.filter(function(e){
    var cb = document.getElementById('ev_'+e.replace(/\./g,'_'));
    return cb && cb.checked;
  });
  whMsg('Registering…');
  whApi('/v1/webhooks', {
    method:'POST',
    headers:{'Content-Type':'application/json'},
    body: JSON.stringify({url:url, events:events.length?events:null,
                          label:document.getElementById('whlabel').value.trim()})
  }).then(function(r){ return r.json().then(function(d){ return {ok:r.ok, d:d}; }); })
    .then(function(res){
      if(!res.ok){
        whMsg(res.d.error ? res.d.error.message : 'Registration failed', true);
        return;
      }
      whMsg('Registered. Alerts will POST to ' + res.d.webhook.url);
      document.getElementById('whurl').value = '';
      document.getElementById('whlabel').value = '';
      loadWebhooks();
    }).catch(function(e){ whMsg(e.message, true); });
};

// ── demo mode ──────────────────────────────────────────────────────────────
// The same page and the same renderer, fed by a synthetic fixture instead of a
// workspace. No credential is involved, so nothing can be leaked or written.
if (__DEMO__) {
  document.getElementById('gate').style.display = 'none';
  var banner = document.createElement('div');
  banner.className = 'muted';
  banner.style.cssText = 'border:1px solid #21262d;border-radius:8px;' +
    'padding:10px 12px;margin-bottom:14px';
  banner.innerHTML = '<b>Read-only demo.</b> Synthetic data — no workspace, ' +
    'no key, nothing written. This is the same dashboard you get with your own ' +
    'data: <a href="/quickstart">connect a real agent</a>.';
  var app = document.getElementById('app');
  app.insertBefore(banner, app.firstChild);
  document.getElementById('csv').style.display = 'none';
  // Demo mode has no workspace and no key, so the webhook form has nothing to
  // authenticate as. Show the section (it is part of the product surface) but
  // disable the controls rather than presenting a button that cannot work.
  ['whurl','whlabel','whadd'].forEach(function(id){
    var el = document.getElementById(id);
    if (el) { el.disabled = true; el.style.opacity = '.5'; }
  });
  whMsg('Read-only demo — register webhooks from your own workspace dashboard.', false);
  fetch('/v1/demo/summary').then(function(r){ return r.json(); }).then(render)
    .catch(function(e){ document.getElementById('gateerr').textContent = e.message; });
}
</script></body></html>"""


_DASH_HEADERS = {
    "Content-Security-Policy": CSP,
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Cache-Control": "no-store",
}


def dashboard_html(demo: bool = False) -> str:
    return _PAGE.replace("__DEMO__", "true" if demo else "false")


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard():
    """The page itself is static: it holds no data and needs no auth. The key is
    pasted in the browser and sent as a header, so there is nothing to leak in
    the HTML, the URL, or a cache."""
    return HTMLResponse(dashboard_html(False), headers=_DASH_HEADERS)


@router.get("/demo", response_class=HTMLResponse)
def demo():
    """A populated dashboard with no signup and no credential.

    Rendered from a fixture rather than a seeded production workspace: there is
    no workspace-deletion path in the API, so a 'demo workspace' would be
    permanent, and it would inflate the counters that /stats serves."""
    import metrics
    try:
        metrics.record_event("demo_view")
    except Exception:
        pass
    return HTMLResponse(dashboard_html(True), headers=_DASH_HEADERS)


@router.get("/v1/demo/summary")
def demo_summary():
    """The fixture the demo page renders. Public, synthetic, read-only — and it
    returns the same shape as /v1/workspace/summary so the two cannot drift."""
    import site_pages
    return site_pages.demo_summary()


class SimulateRequest(BaseModel):
    monthly_cap_cents: int
    already_spent_cents: int
    estimated_call_cents: int


@router.post("/v1/demo/simulate")
def demo_simulate(req: SimulateRequest):
    """D-1250 — the interactive blocked-call proof. Public, no auth, no
    workspace, no agent_id, nothing written to disk: it takes three numbers
    and returns the SAME message shape routes_proxy.py's real
    `_pre_call_check` returns on a real proxied call. This is not a second,
    friendlier copy of that logic — it is the identical "would spent + estimate
    exceed cap" comparison and the identical message template, so the proof a
    visitor sees here cannot drift from what the product actually does."""
    import metrics
    spent = req.already_spent_cents
    cap = req.monthly_cap_cents
    estimate = req.estimated_call_cents
    try:
        metrics.record_event("simulator_run")
    except Exception:
        pass
    if cap > 0 and spent + estimate > cap:
        try:
            metrics.record_event("simulator_blocked")
        except Exception:
            pass
        return {
            "blocked": True,
            "status_code": 402,
            "message": (
                f"blocked before the provider: this call's estimated maximum cost "
                f"({estimate} cents) would put this agent over its monthly budget "
                f"({spent} of {cap} cents). Nothing was sent upstream."),
            "code": "budget_exceeded",
        }
    return {"blocked": False, "status_code": 200,
            "message": "within budget — the call would be forwarded to the provider."}


@router.post("/v1/demo/quickstart-copy")
def quickstart_copy_beacon():
    """A one-field beacon: the quickstart page's copy buttons call this so the
    funnel can see 'copied the install line' between 'viewed the page' and
    'made a real call' — no auth, no body, nothing stored beyond the count."""
    import metrics
    try:
        metrics.record_event("quickstart_copy")
    except Exception:
        pass
    return {"ok": True}
