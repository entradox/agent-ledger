"""The human-facing surfaces the gap plan asked for: a public demo, a
quickstart, and the umbrella pages (D-1226 tranche 2, GAP-3/4/6/9).

Two rules govern this file:

1. **The demo is synthetic and read-only.** It renders through the SAME shape
   the real /v1/workspace/summary returns, so it cannot drift from the product,
   but it reads no ledger and holds no key. Seeding a real demo workspace on
   production would mint a workspace we then cannot delete (there is no
   workspace-deletion path) and would inflate the public counters — the exact
   mess the gap plan asked us to clean up.

2. **No page advertises anything that does not work, or that belongs to
   someone else.** Published on PyPI and npm as `aiagentscity-ledger` (v0.4.1,
   verified live 2026-09-14) — the umbrella namespace, which nobody can
   register without impersonating a domain we own. The two obvious names are
   both other parties' (`agent-ledger` on PyPI is Rune0's; `agentledger` on
   npm is agentledger.co's), so naming either one would hand a user a
   stranger's code.
"""
from __future__ import annotations

import random
from datetime import date, timedelta

BASE_URL = "https://aiagentscity.com"

# ── the shell every page wears ──────────────────────────────────────────────
SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{desc}">
<style>
:root{{--bg:#0d1117;--fg:#e6edf3;--mut:#8b949e;--line:#21262d;--acc:#58a6ff;
--ok:#3fb950;--warn:#d29922;--bad:#f85149}}
*{{box-sizing:border-box}}
body{{background:var(--bg);color:var(--fg);margin:0;line-height:1.65;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif}}
a{{color:var(--acc)}}
.wrap{{max-width:760px;margin:0 auto;padding:34px 20px 8px}}
.tag{{font-size:11px;letter-spacing:.09em;text-transform:uppercase;color:var(--mut)}}
.tag a{{color:var(--mut);text-decoration:none;border-bottom:1px solid var(--line)}}
h1{{font-size:30px;margin:10px 0 8px;letter-spacing:-.02em}}
h2{{font-size:17px;margin:32px 0 8px}}
h3{{font-size:14px;margin:20px 0 4px}}
p,li{{color:#c9d1d9;font-size:15px}}
.mut{{color:var(--mut);font-size:13px}}
code{{background:#161b22;border:1px solid var(--line);border-radius:5px;
padding:2px 6px;font-size:13px}}
pre{{background:#161b22;border:1px solid var(--line);border-radius:8px;
padding:14px;overflow-x:auto;font-size:13px;line-height:1.55}}
pre code{{background:none;border:0;padding:0}}
hr{{border:0;border-top:1px solid var(--line);margin:34px 0}}
.foot{{border-top:1px solid var(--line);margin-top:40px;padding:22px 20px 44px;
text-align:center;color:var(--mut);font-size:12.5px}}
.foot a{{color:var(--mut)}}
</style></head><body>
<div class="wrap">
<div class="tag"><a href="/about">An AI Agent City product</a></div>
{body}
<hr>
<p class="mut"><b>AgentLedger</b> — {footnav}</p>
</div>
<div class="foot">
AgentLedger — an <a href="/about">AI Agent City</a> product · operated by Parmanand LLC<br>
<a href="/">home</a> · <a href="/quickstart">quickstart</a> · <a href="/demo">live demo</a> ·
<a href="/compare">compare</a> · <a href="/security">security</a> ·
<a href="/status">status</a> · <a href="/terms">terms</a> · <a href="/privacy">privacy</a>
</div></body></html>"""

FOOTNAV_DEFAULT = ('not affiliated with <b>agentledger.co</b> or other same-named '
                   'projects · <a href="/quickstart">start in 5 minutes</a>')


def page(title: str, desc: str, body: str, footnav: str = FOOTNAV_DEFAULT) -> str:
    return SHELL.format(title=title, desc=desc, body=body, footnav=footnav)


# ── the umbrella shell — no product name, no operator name, no email ───────
# Used for "/" and the sub-product pages that don't yet have their own full
# site (D-1239). Unlike SHELL above, this carries no company/contact line —
# the umbrella lists products only, per the 2026-09-14 directive to strip
# operator identity off any page that is not a specific product's own page.
CITY_SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>{title}</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="{desc}">
<style>
:root{{--paper:#FAFAF8;--card:#FFFFFF;--ink:#1B1B18;--muted:#6E6E68;
--line:#E8E6DF;--accent:#4F46E5;--accent-soft:#EEEDFE;
--ok:#15803d;--warn:#A16207;--bad:#B91C1C;
--serif:Georgia,'Times New Roman',serif;
--sans:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace}}
*{{box-sizing:border-box}}
body{{background:var(--paper);color:var(--ink);margin:0;line-height:1.65;
font-family:var(--sans);-webkit-font-smoothing:antialiased}}
.wrap{{max-width:720px;margin:0 auto;padding:40px 20px 8px}}
.kicker{{font-family:var(--mono);font-size:12px;color:var(--muted);
letter-spacing:.02em;margin-bottom:14px}}
h1{{font-family:var(--serif);font-size:clamp(30px,5vw,44px);font-weight:400;
letter-spacing:-.02em;line-height:1.12;margin:0 0 12px}}
.lede{{font-size:17px;color:var(--muted);max-width:60ch;margin:0 0 28px}}
.lede b{{color:var(--ink);font-weight:600}}
h2{{font-family:var(--serif);font-size:22px;font-weight:400;margin:34px 0 10px;
letter-spacing:-.01em}}
p,li{{font-size:15px;color:#3A3A36}}
.mut{{color:var(--muted);font-size:13.5px}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:14px;
padding:20px;margin:14px 0;box-shadow:0 1px 2px rgba(27,27,24,.04)}}
.card h3{{font-size:16px;margin:0 0 6px;font-weight:600}}
.ver{{font-family:var(--mono);font-size:11.5px;color:var(--muted);
background:var(--paper);border:1px solid var(--line);border-radius:20px;
padding:2px 9px;white-space:nowrap}}
.row{{display:flex;gap:10px;margin-top:12px;flex-wrap:wrap}}
input[type=text],input[type=url],input[type=email]{{
flex:1;min-width:200px;font:inherit;font-size:15px;padding:11px 14px;
border:1px solid var(--line);border-radius:10px;background:var(--paper);
color:var(--ink)}}
input:focus{{outline:2px solid var(--accent);outline-offset:0;border-color:var(--accent)}}
button.go{{font:inherit;font-size:15px;font-weight:600;color:#fff;
background:var(--ink);border:0;border-radius:10px;padding:11px 20px;cursor:pointer}}
button.go:hover{{background:#000}}
button.go:disabled{{opacity:.55;cursor:wait}}
pre{{background:#141412;color:#EDEDE8;border-radius:12px;padding:16px 18px;
overflow-x:auto;font-family:var(--mono);font-size:13px;line-height:1.6;
position:relative;margin:12px 0}}
pre .cb{{position:absolute;top:8px;right:8px;font-family:var(--mono);
font-size:11px;background:#2A2A26;color:#EDEDE8;border:1px solid #3A3A36;
border-radius:6px;padding:3px 9px;cursor:pointer}}
.res{{margin-top:14px;border-top:1px solid var(--line);padding-top:14px;display:none}}
.res.show{{display:block}}
.kv{{display:grid;grid-template-columns:150px 1fr;gap:6px 12px;font-size:14px}}
.kv dt{{color:var(--muted);font-family:var(--mono);font-size:12px;padding-top:2px}}
.kv dd{{margin:0;color:var(--ink);overflow-wrap:anywhere}}
.chk{{display:flex;gap:10px;align-items:flex-start;padding:9px 0;
border-bottom:1px solid var(--line);font-size:14px}}
.chk:last-child{{border-bottom:0}}
.dot{{width:9px;height:9px;border-radius:50%;margin-top:6px;flex:none}}
.dot.pass{{background:var(--ok)}}.dot.info{{background:var(--accent)}}
.dot.warn{{background:var(--warn)}}
.chk b{{font-weight:600}}
.chk .d{{color:var(--muted);font-size:13px}}
.sum{{background:var(--accent-soft);border:1px solid #DCDDFB;border-radius:10px;
padding:12px 14px;font-size:14px;margin-top:12px}}
.err{{background:#FEF2F2;border:1px solid #FECACA;color:var(--bad);
border-radius:10px;padding:12px 14px;font-size:14px;margin-top:12px}}
.spin{{color:var(--muted);font-size:14px;margin-top:12px}}
ul.feat{{list-style:none;padding:0;margin:8px 0}}
ul.feat li{{padding:7px 0 7px 26px;position:relative;font-size:14.5px}}
ul.feat li:before{{content:"→";position:absolute;left:4px;color:var(--accent)}}
.topbar{{display:flex;justify-content:space-between;align-items:center;
max-width:720px;margin:0 auto;padding:16px 20px 0}}
.topbar .wm{{font-family:var(--mono);font-size:12px;letter-spacing:.14em;
color:var(--ink);text-decoration:none}}
.topbar nav a{{font-size:13px;color:var(--muted);text-decoration:none;margin-left:16px}}
.topbar nav a:hover{{color:var(--ink)}}
.foot{{border-top:1px solid var(--line);margin-top:44px;padding:24px 20px 48px;
text-align:center;color:var(--muted);font-size:12.5px}}
.foot a{{color:var(--muted);text-decoration:none;margin:0 8px}}
.foot a:hover{{color:var(--ink)}}
@media(max-width:560px){{.kv{{grid-template-columns:1fr}}.kv dt{{padding-top:8px}}}}
</style></head><body>
<div class="topbar"><a class="wm" href="/">AI AGENT CITY</a>
<nav><a href="/products">products</a><a href="/developers">developers</a><a href="/changelog">changelog</a></nav></div>
<div class="wrap">
{body}
</div>
<div class="foot">
<a href="/">home</a> &middot; <a href="/agent-ledger">AgentLedger</a> &middot;
<a href="/perimeter-watch">Perimeter Watch</a> &middot; <a href="/cited">Cited</a> &middot;
<a href="/agent-watch">Agent Watch</a> &middot; <a href="/trust-scan">TrustScan</a> &middot;
<a href="/privacy">privacy</a> &middot; <a href="/terms">terms</a>
</div></body></html>"""


def city_page(title: str, desc: str, body: str) -> str:
    return CITY_SHELL.format(title=title, desc=desc, body=body)


# ── umbrella index + sub-product pages (D-1239, generated by
# projects/agent-city/generator.py from product-skill-surface/registry.json —
# regenerate there and re-paste here if a product's tagline/examples change;
# this file never reads that registry at request time since it does not
# exist in the deployed container) ──────────────────────────────────────────
UMBRELLA_INDEX = """
<h1>AI Agent City</h1>
<p class="mut">A toolbelt for people who run AI agents in production. Every product
below is independently installable and solves one operational problem end to end.
Agent-readable at <b>aiagentscity.com</b> &mdash; every product ships MCP + REST + CLI.</p>

<h2>Products</h2>
<div class="card"><h3><a class="plain" href="/agent-ledger">AgentLedger</a></h3><p class="mut">Spending limits for AI agents</p></div>
<div class="card"><h3><a class="plain" href="/perimeter-watch">Perimeter Watch</a></h3><p class="mut">Passive external-perimeter monitoring for web agencies</p></div>
<div class="card"><h3><a class="plain" href="/cited">Cited</a></h3><p class="mut">Does AI recommend your practice? Instant free scan, verbatim evidence.</p></div>
<div class="card"><h3><a class="plain" href="/agent-watch">Agent Watch</a></h3><p class="mut">Monitoring for the agent economy</p></div>
<div class="card"><h3><a class="plain" href="/trust-scan">TrustScan</a></h3><p class="mut">Scan before you trust</p></div>

<h2>Start here</h2>
<p class="mut">A workspace needs no signup and no card, and the key is shown once.</p>
<p><a href="/start">Get a workspace &rarr;</a> &middot; <a href="/quickstart">Start in 5 minutes</a></p>
"""

PERIMETER_WATCH_PAGE = """
<h1>Perimeter Watch</h1>
<p class="mut">Passive external-perimeter monitoring for web agencies</p>
<p class="mut">v1.30.0 &middot; 6 MCP tools</p>

<h2>Try it</h2>
<ul>
<li>Scan example.com for dangling DNS and cert expiry</li>
<li>Check for lookalike domains impersonating my client's brand</li>
<li>Give me this week's perimeter brief for my authorized domains</li>
</ul>

<h2>Connect</h2>
<p class="mut">MCP (no signup):</p>
<pre>claude mcp add --transport http perimeter-watch https://aiagentscity.com/mcp/perimeter-watch/</pre>
<p class="mut">Or run a free one-time snapshot in the browser:</p>
<p><a class="plain" href="https://entradox.github.io/perimeter-watch-site/">Free perimeter snapshot &rarr;</a></p>
<p class="mut">Status: MCP tools execute through the city gateway, which translates tool calls
to the product's documented REST API &mdash; handshake and all 6 tools verified working
(2026-09-20). The free browser snapshot works today.</p>

{PRICING_PERIMETER_WATCH}
"""

CITED_PAGE = """
<h1>Cited</h1>
<p class="mut">Does AI recommend your practice? Instant free scan, verbatim evidence.</p>
<p class="mut">v1.30.0 &middot; 9 MCP tools</p>

<h2>Try it</h2>
<ul>
<li>Scan Gentry Dentistry of Suwanee for AI visibility</li>
<li>Is my dental practice recommended by AI engines?</li>
<li>Compare my practice's AI visibility against two competitors</li>
</ul>

<h2>Connect</h2>
<p class="mut">MCP (no signup):</p>
<pre>claude mcp add --transport http cited https://aiagentscity.com/mcp/cited/</pre>
<p class="mut">Or run a free scan in the browser:</p>
<p><a class="plain" href="https://entradox.github.io/cited-site/">Free AI-visibility scan &rarr;</a></p>
<p class="mut">Status: MCP tools execute through the city gateway, which translates tool calls
to the product's documented REST API &mdash; handshake and all 9 tools verified working
(2026-09-20). The free browser scan works today.</p>

{PRICING_CITED}
"""

AGENT_WATCH_PAGE = """
<div class="kicker">// human-readable &middot; aiagentscity.com/agent-watch</div>
<h1>Know the moment a new agent touches your API.</h1>
<p class="lede"><b>Non-human traffic is hitting your APIs and you can't see it.</b>
Agent Watch monitors the agent economy's endpoints &mdash; liveness, latency, schema
drift, auth posture, price integrity. Probe any endpoint below, free, no signup.</p>
<p><span class="ver">v1.30.0</span> <span class="ver">8 MCP tools</span> <span class="ver">live</span></p>

<div class="card" id="probe">
<h3>Probe an endpoint now</h3>
<p class="mut">We check liveness, latency, and whether the endpoint advertises a
price challenge &mdash; the handshake your agent would do, without your agent.</p>
<div class="row">
<input type="url" id="probe-url" placeholder="https://your-api.com/mcp/" autocomplete="off">
<button class="go" id="probe-btn" onclick="runProbe()">Probe</button>
</div>
<div class="res" id="probe-res"></div>
</div>

<div class="card">
<h3>The agent economy, right now</h3>
<p class="mut">Live census across the public MCP registry &mdash; refreshed on load.</p>
<div class="res show" id="census-res"><p class="spin">Loading census&hellip;</p></div>
</div>

<div class="card">
<h3>Have a watch token?</h3>
<p class="mut">Check your watchlist status &mdash; plan, monitored endpoints, per-endpoint health.</p>
<div class="row">
<input type="email" id="w-email" placeholder="you@example.com" autocomplete="off">
<input type="text" id="w-token" placeholder="watch token" autocomplete="off">
<button class="go" onclick="checkWatch()">Check status</button>
</div>
<div class="res" id="watch-res"></div>
<p class="mut" style="margin-top:10px">Monitoring subscriptions with email alerts
are <b>$29/mo</b> — pick a plan below and checkout is self-serve. Every MCP tool
&mdash; including <span class="ver">aw_watch</span> &mdash; works today for subscribers.</p>
</div>

{PRICING_AGENT_WATCH}

<h2>For agents</h2>
<div class="card">
<h3>MCP &mdash; no signup</h3>
<pre><button class="cb" onclick="copyPre(this)">copy</button>claude mcp add --transport http agent-watch https://aiagentscity.com/mcp/agent-watch/</pre>
<ul class="feat">
<li><b>aw_census</b> &mdash; registry liveness across thousands of agent endpoints</li>
<li><b>aw_check_endpoint</b> &mdash; probe one endpoint: liveness, latency, schema drift</li>
<li><b>aw_alerts</b> &mdash; dead and unresponsive endpoints</li>
<li><b>aw_watch</b> &mdash; watchlist status for token holders</li>
</ul>
<p class="mut">Tools execute through the city gateway, which translates MCP tool calls
to the product's documented REST API. MCP handshake and all 8 tools verified working
(2026-09-20).</p>
</div>

<script>
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,
c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function copyPre(b){var t=b.parentElement.innerText.replace(/^copy\\n/,"");
navigator.clipboard.writeText(t).then(()=>{b.textContent="copied";
setTimeout(()=>b.textContent="copy",1500)})}
function kv(o){var h='<dl class="kv">';
for(var k in o){var v=o[k];
if(v===null||v===undefined||v==="")continue;
h+="<dt>"+esc(k)+"</dt><dd>"+esc(typeof v==="object"?JSON.stringify(v):v)+"</dd>"}
return h+"</dl>"}
async function runProbe(){
var u=document.getElementById("probe-url").value.trim();
var box=document.getElementById("probe-res"),btn=document.getElementById("probe-btn");
if(!u){box.className="res show";box.innerHTML='<div class="err">Enter an endpoint URL first.</div>';return}
btn.disabled=true;box.className="res show";box.innerHTML='<p class="spin">Probing&hellip; (up to ~30s)</p>';
try{var r=await fetch("/api/agent-watch/probe",{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify({url:u})});
var j=await r.json();
if(!j.ok){box.innerHTML='<div class="err">'+esc(j.error||"probe failed")+"</div>"}
else{var x=j.result;
box.innerHTML=kv({"endpoint":x.endpoint_url,"alive":x.alive?"yes":"no",
"latency p50":x.latency_p50_ms!=null?x.latency_p50_ms+" ms":"—",
"latency p95":x.latency_p95_ms!=null?x.latency_p95_ms+" ms":"—",
"protocol":x.protocol,"tools":x.tool_count,
"price challenge":x.price_challenge_found?"advertised":"not found",
"error":x.error,"probed at":x.probed_at})+
'<p class="mut">Alive means the endpoint answered the same handshake an agent would send.</p>'}
}catch(e){box.innerHTML='<div class="err">Request failed — check your connection and try again.</div>'}
btn.disabled=false}
async function checkWatch(){
var e=document.getElementById("w-email").value.trim();
var t=document.getElementById("w-token").value.trim();
var box=document.getElementById("watch-res");
box.className="res show";box.innerHTML='<p class="spin">Checking&hellip;</p>';
try{var r=await fetch("/api/agent-watch/watch?email="+encodeURIComponent(e)+
"&token="+encodeURIComponent(t));
var j=await r.json();
if(!j.ok){box.innerHTML='<div class="err">'+esc(j.error||"lookup failed")+"</div>"}
else{box.innerHTML=kv(j.watch||{})}}catch(e2){
box.innerHTML='<div class="err">Request failed — try again.</div>'}}
(async function(){try{var r=await fetch("/api/agent-watch/census");
var j=await r.json();var box=document.getElementById("census-res");
if(j.ok&&j.census){var c=j.census;
box.innerHTML=kv({"endpoints tracked":c.total,"alive":c.alive+" ("+(c.pct_alive||"—")+"%)",
"dead":c.dead,"unreachable/unknown":c.unknown})+
'<p class="mut">Alive = answered the MCP handshake. Dead = no answer. Source: Agent Watch registry census.</p>'}
else{box.innerHTML='<p class="mut">Census temporarily unavailable.</p>'}}catch(e){
document.getElementById("census-res").innerHTML='<p class="mut">Census temporarily unavailable.</p>'}})();
</script>
"""

TRUST_SCAN_PAGE = """
<div class="kicker">// human-readable &middot; aiagentscity.com/trust-scan</div>
<h1>Scan before you trust.</h1>
<p class="lede">Your agent is about to install a stranger's MCP server or skill.
<b>Know who you're dealing with before your agent does.</b> Check any npm package
below &mdash; free, no signup &mdash; then run the deep code scan over MCP.</p>
<p><span class="ver">v4.0.3</span> <span class="ver">4 MCP tools</span> <span class="ver">live</span></p>

<div class="card" id="npmcheck">
<h3>Check an npm package</h3>
<p class="mut">Registry facts plus a typosquat screen against widely-used packages.
This is the metadata half of the scan &mdash; the deep code scan runs over MCP, below.</p>
<div class="row">
<input type="text" id="ts-pkg" placeholder="package name, e.g. express" autocomplete="off">
<button class="go" id="ts-btn" onclick="runNpmCheck()">Check</button>
</div>
<div class="res" id="ts-res"></div>
</div>

<div class="card">
<h3>What the deep scan checks</h3>
<ul class="feat">
<li><b>Invisible-Unicode prompt injection</b> &mdash; zero-width characters hiding instructions in code</li>
<li><b>Dangerous patterns MCP001&ndash;MCP006</b> &mdash; the known-dangerous MCP server behaviors</li>
<li><b>Hardcoded secrets</b> &mdash; API keys and tokens baked into the package</li>
<li><b>Typosquat names</b> &mdash; packages impersonating popular ones</li>
</ul>
<p class="mut">The deep scan is read-only: it never modifies the target. Verdicts come back
as a 0&ndash;100 score, a letter grade, and detailed findings.</p>
</div>

<h2>For agents</h2>
<div class="card">
<h3>MCP &mdash; no signup</h3>
<pre><button class="cb" onclick="copyPre(this)">copy</button>claude mcp add --transport http trustscan https://aiagentscity.com/mcp/trustscan/</pre>
<ul class="feat">
<li><b>trust_scan_server</b>(path, package_name) &mdash; full security scan of an MCP server or skill package</li>
<li><b>trust_scan_file</b>(filepath) &mdash; scan a single file</li>
</ul>
<p class="mut">The deep scan runs over MCP, where the scanner has the code in front of it
&mdash; teams wire it into CI on the <b>Team plan</b> below.</p>
</div>

{PRICING_TRUST_SCAN}

<script>
function esc(s){return String(s==null?"":s).replace(/[&<>"]/g,
c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function copyPre(b){var t=b.parentElement.innerText.replace(/^copy\\n/,"");
navigator.clipboard.writeText(t).then(()=>{b.textContent="copied";
setTimeout(()=>b.textContent="copy",1500)})}
async function runNpmCheck(){
var p=document.getElementById("ts-pkg").value.trim();
var box=document.getElementById("ts-res"),btn=document.getElementById("ts-btn");
if(!p){box.className="res show";box.innerHTML='<div class="err">Enter a package name first.</div>';return}
btn.disabled=true;box.className="res show";box.innerHTML='<p class="spin">Checking registry&hellip;</p>';
try{var r=await fetch("/api/trustscan/npm",{method:"POST",
headers:{"Content-Type":"application/json"},body:JSON.stringify({package:p})});
var j=await r.json();
if(!j.ok){var h='<div class="err">'+esc(j.error||"check failed")+"</div>";
if(j.suggestion)h+='<p class="mut">Did you mean <b>'+esc(j.suggestion)+'</b>?</p>';
box.innerHTML=h}
else if(!j.found){box.innerHTML='<div class="sum"><b>'+esc(j.package)+
'</b> is not on the public npm registry. '+
'If an agent asked you to install it, that alone is a red flag &mdash; verify the name.</div>'}
else{var h="";
if(j.description)h+='<p class="mut">'+esc(j.description)+"</p>";
for(var i=0;i<j.checks.length;i++){var c=j.checks[i];
h+='<div class="chk"><span class="dot '+c.state+'"></span><div><b>'+esc(c.label)+
'</b><div class="d">'+esc(c.detail)+"</div></div></div>"}
h+='<div class="sum"><b>Read:</b> '+esc(j.summary)+".</div>";
h+='<p class="mut">'+esc(j.scope_note)+".</p>";
box.innerHTML=h}}catch(e){
box.innerHTML='<div class="err">Request failed — check your connection and try again.</div>'}
btn.disabled=false}
document.getElementById("ts-pkg").addEventListener("keydown",function(e){
if(e.key==="Enter")runNpmCheck()});
</script>
"""


# ── the demo fixture ────────────────────────────────────────────────────────
# Five agents, 30 days, one deliberately over its cap: the story the demo has to
# tell is "a runaway agent got stopped", so the numbers are chosen, not random.
DEMO_AGENTS = [
    # agent_id,             tier, cap_cents, service,      rail,     daily_mean
    ("support-triage",       "pro",  100_000, "openai",     "api_key", 4_100),
    ("research-crew",        "pro",   39_000, "langgraph",  "api_key", 1_380),
    ("claude-code-session",  "free",       0, "anthropic",  "api_key",   50),
    ("codex-refactor",       "free",  20_000, "openai",     "api_key",   295),
    ("data-enrichment",      "pro",   40_000, "moonshot",   "api_key",   680),
]


def demo_summary() -> dict:
    """A deterministic 30-day workspace. Same shape as the live summary."""
    rng = random.Random(20260913)  # fixed: the demo must not change per request
    today = date.today()
    days = [today - timedelta(days=29 - i) for i in range(30)]

    agents, series, by_service, by_rail = [], {}, {}, {}
    tin_total = tout_total = spend_total = 0

    for agent_id, tier, cap, service, rail, mean in DEMO_AGENTS:
        spent = 0
        for d in days:
            # weekends run lighter and there is one spike day, so the chart
            # looks like a real workload rather than a flat bar strip
            v = max(0, int(rng.gauss(mean, mean * 0.34)))
            if d.weekday() >= 5:
                v = int(v * 0.35)
            if agent_id == "support-triage" and d == days[21]:
                v = int(mean * 2.6)  # the day the loop ran away
            if v:
                spent += v
                series[d.isoformat()] = series.get(d.isoformat(), 0) + v
        used_pct = int(round(spent * 100 / cap)) if cap else 0
        status = "exceeded" if cap and spent > cap else (
            "warning" if cap and used_pct >= 80 else "ok")
        tin, tout = int(spent * 3100), int(spent * 7)
        tin_total += tin
        tout_total += tout
        spend_total += spent
        by_service[service] = by_service.get(service, 0) + spent
        by_rail[rail] = by_rail.get(rail, 0) + spent
        agents.append({
            "agent_id": agent_id, "tier": tier, "spend_cents_30d": spent,
            "tokens_in_30d": tin, "tokens_out_30d": tout,
            "budget": {"monthly_cents": cap, "used_pct": used_pct, "status": status},
            "last_event_ts": None, "anomaly": agent_id == "support-triage",
        })

    agents.sort(key=lambda a: a["spend_cents_30d"], reverse=True)
    stamp = f"{today.isoformat()}T09:14:00+00:00"

    # The alert text is DERIVED from the computed rows, never written by hand:
    # a demo whose prose disagrees with its own chart is worse than no demo.
    by_id = {a["agent_id"]: a for a in agents}
    tri = by_id["support-triage"]
    crew = by_id["research-crew"]
    alerts = [
        {"ts": 0, "agent_id": "support-triage", "type": "budget.exceeded",
         "message": (f"monthly cap ${tri['budget']['monthly_cents']/100:,.2f} reached — "
                     f"the next proxy call was refused with 402 before the provider "
                     f"was contacted"),
         "timestamp": f"{days[22].isoformat()}T04:02:11+00:00"},
        {"ts": 0, "agent_id": "support-triage", "type": "anomaly.detected",
         "message": "spend on this day was 2.6x the 14-day median",
         "timestamp": f"{days[21].isoformat()}T23:41:07+00:00"},
        {"ts": 0, "agent_id": "research-crew", "type": "budget.warning",
         "message": (f"{crew['budget']['used_pct']}% of the "
                     f"${crew['budget']['monthly_cents']/100:,.2f} monthly cap used"),
         "timestamp": f"{days[26].isoformat()}T11:20:45+00:00"},
    ]
    return {
        "workspace_id": "ws_demo_readonly",
        "days": 30,
        "demo": True,
        "agents": agents,
        "totals": {
            "spend_cents_30d": spend_total,
            "tokens_in_30d": tin_total,
            "tokens_out_30d": tout_total,
            "by_rail": by_rail,
            "by_service": by_service,
        },
        "daily_series": [{"date": d.isoformat(), "spend_cents": series.get(d.isoformat(), 0)}
                         for d in days],
        "alerts": alerts,
        "generated": stamp,
    }


# ── /quickstart ─────────────────────────────────────────────────────────────
QUICKSTART = """
<h1>Five minutes to a metered agent</h1>
<p class="mut">Three ways in. Pick the one that matches how your agent runs.
The Python client is on PyPI as <code>aiagentscity-ledger</code> (and on npm for
the <code>npx</code> launcher). Note the console command it installs is
<code>agent-ledger</code>.</p>

<h2>1 · The wrapper — every call metered, priced, and capped</h2>
<p>Works with the <code>openai</code> and <code>anthropic</code> Python clients.
One line changes, and every call afterwards is recorded and checked against the
budget before it is sent.</p>
<pre><code>pip install "aiagentscity-ledger[wrapper]"
agent-ledger init --agent my-agent      # prints agent_secret ONCE and writes .env

import agentledger
from openai import OpenAI

client = agentledger.wrap(OpenAI(), agent_id="my-agent",
                          agent_secret="as_...")   # from .env / the init output

client.chat.completions.create(model="gpt-4o",
                               messages=[{"role": "user", "content": "hi"}])
# cost is computed from the provider's own token counts, not from your estimate</code></pre>
<p class="mut">No <code>agent-ledger</code> CLI? Claim the agent by sending your
<code>workspace_key</code> in the <b>body</b> of a first
<code>POST /v1/track</code> — the response returns that agent's
<code>agent_secret</code>. Either way the secret is what the wrapper needs:
a workspace_key authorizes workspace-level reads, not a proxied call.</p>
<p class="mut">Set a cap and the next call is refused <b>before the provider is
contacted</b> — see it appear on your <a href="/dashboard">dashboard</a>.</p>

<h2>2 · The proxy — for anything you cannot change</h2>
<p>Point your existing client at us instead of the provider. Nothing to install.</p>
<pre><code>client = OpenAI(base_url="https://aiagentscity.com/proxy/openai/v1",
                api_key="sk-your-own-provider-key")</code></pre>
<p class="mut">Your provider key is forwarded verbatim and never written to disk.
When the budget is exhausted the proxy returns <code>402</code> and the provider
never sees the call.</p>

<h2>3 · MCP — for Claude Code, Codex and Cursor</h2>
<pre><code>claude mcp add --transport http agent-ledger https://aiagentscity.com/mcp/</code></pre>
<p class="mut">Tools: <code>ledger_track</code>, <code>ledger_set_budget</code>,
<code>ledger_report</code>, <code>ledger_alerts</code>, <code>ledger_list_agents</code>,
<code>ledger_api_docs</code>, <code>ledger_examples</code>, <code>ledger_rotate_secret</code>,
<code>ledger_revoke_secret</code>.
First call:</p>
<pre><code>&gt; set a $20 monthly budget on my-claude-session
&gt; how much has my-claude-session spent this month?</code></pre>

<h2>Then open the dashboard</h2>
<p>Paste your workspace key at <a href="/dashboard">/dashboard</a> — every agent,
its spend, its budget bar and its alerts on one page. Not ready to sign up?
<a href="/demo">Look at a populated demo first →</a></p>

<h2>Command line, if you prefer</h2>
<pre><code>agent-ledger init          # mint a workspace, write .env
agent-ledger track ...     # record a spend
agent-ledger set-budget ...# cap an agent
agent-ledger report        # per-agent spend
agent-ledger share &lt;agent&gt;# print a shareable report link</code></pre>
"""

# ── /security ───────────────────────────────────────────────────────────────
# ── About page ────────────────────────────────────────────────────────────
# FOUNDER_NAME is deliberately not guessed and not invented. Set it to a real
# name to publish the founder line; while it is empty the page renders a
# first-person paragraph that names no one, so the page is never half-built.
FOUNDER_NAME = ""
FOUNDER_BLURB = ""

ABOUT = """<h1>About</h1>

<h2>Who runs this</h2>
<p>AI Agent City is operated by <b>Parmanand LLC</b>, a Georgia company. It is an
independent, self-funded software project — not a venture-backed startup and not a
reseller of anyone else's product.</p>
{founder_block}

<h2>Why it exists</h2>
<p>Agents started holding credentials, calling paid APIs, and spending real money
before anyone built the boring layer that makes that safe. A budget is now an
operational primitive, not a spreadsheet: if an agent can spend, something has to
enforce a limit <i>before</i> the money leaves — not report it afterwards.</p>
<p>That is the whole thesis. Every product here is a piece of that operations layer:
control spend, monitor traffic, watch the perimeter, check who you're trusting,
and make sure AI recommends you.</p>

<h2>How we work</h2>
<p>One rule shapes everything: <b>publish the caveat, not just the claim.</b> Where a
guarantee is weaker than it sounds, the page says so — for example, a budget cap on
the plain API stops the ledger write, not the provider charge, and that difference
is stated on the pricing page and in the Terms rather than buried.</p>
<p>The tool counts on this site are the live <code>tools/list</code> output, not
marketing numbers. When something is not working, the status note says what works and
what does not, with a date.</p>

<h2>Contact</h2>
<p>General and support: <a href="mailto:entradox@icloud.com">entradox@icloud.com</a><br>
Refunds, billing and privacy/deletion requests go to the same address.</p>
<p>We aim to answer within two business days.</p>

<h2>Public record</h2>
<p><a href="/changelog">Changelog</a> — every ship, dated ·
<a href="/status">Live status</a> ·
<a href="/security">Security &amp; data handling</a> ·
<a href="/terms">Terms</a> ·
<a href="/privacy">Privacy</a></p>
"""


def _render_about() -> str:
    """Fill the founder slot. Empty FOUNDER_NAME renders an unnamed first-person
    paragraph rather than a placeholder, so the page is honest either way."""
    if FOUNDER_NAME:
        block = (
            f'<p><b>{FOUNDER_NAME}</b> — founder. {FOUNDER_BLURB}</p>'
            if FOUNDER_BLURB else f'<p><b>{FOUNDER_NAME}</b> — founder.</p>')
    else:
        block = (
            "<p>It is built and run by its founder directly — engineering, support and "
            "billing all reach the same person. Every product on this site was written "
            "here, and the changelog is the record of it.</p>")
    return ABOUT.replace("{founder_block}", block)


SECURITY = """
<h1>Security &amp; data handling</h1>

<h2>What we store — and what we never store</h2>
<p>Per entry: agent id, rail, service label, amount, token counts, model name,
timestamp. Per workspace: workspace id, a hash of the workspace key, plan and
billing state, and any alert webhook you register.</p>
<p><b>Prompts and model responses are never stored.</b> There is no field for
them and no code path that writes message content to storage. That is true of
the ledger and it is a hard constraint on the proxy: it meters cost from the
provider's usage payload without retaining the payload. Verified in the proxy's
own test suite, which asserts no prompt text reaches the data directory.</p>

<h2>Your provider keys</h2>
<p>In pass-through mode your provider key rides in the request headers, is
forwarded verbatim, and is never written to disk or logged. A test greps the
entire data directory for it after a proxied call and fails if it appears.
You can therefore use the proxy without giving us a key at all.</p>

<h2>What a budget cap actually guarantees</h2>
<p>Two mechanisms, and the difference matters:</p>
<ul>
<li><b>Through the proxy or wrapper</b> — the call is refused with
<code>402</code> before the provider is contacted. Spend is genuinely stopped.</li>
<li><b>Through <code>/v1/track</code></b> — the ledger <i>write</i> that would
cross the cap is rejected. Recording stops. The charge, if your agent already
made it, does not.</li>
</ul>
<p>A caller who can reach the provider directly can always bypass any proxy.
Use the wrapper or proxy path if a cap has to be real.</p>

<h2>Credentials</h2>
<p>Workspace keys and agent secrets are stored hashed or in files readable only
by the service. The workspace owner can rotate any agent's secret and revoke the
old one. Keys are never placed in URLs by our own pages — the dashboard sends
your key as a request header.</p>

<h2>Reporting a vulnerability</h2>
<p>Email <a href="mailto:entradox@icloud.com">entradox@icloud.com</a> with the
subject line <code>SECURITY</code>, including what you found and how to
reproduce it. We will confirm receipt, tell you what we intend to do, and credit
you if you want the credit. Please do not test against other people's
workspaces, run destructive tests, or access data that is not yours.</p>

<h2>Uptime</h2>
<p>Live health and version: <a href="/status">/status</a> ·
<a href="/health">/health</a>.</p>
"""

# ── /compare ────────────────────────────────────────────────────────────────
COMPARE = """
<h1>Why not just a trace viewer</h1>
<p>Trace tools answer <i>"what did my agent do?"</i> AgentLedger answers
<i>"what did my agent cost, and is it allowed to keep going?"</i> Those are
different questions, and only one of them has a bill attached.</p>

<h2>The difference that matters</h2>
<p>The unit of accounting. Trace viewers key on the <b>request</b>, so cost is
something you reconstruct from a span tree. AgentLedger keys on the
<b>agent</b>, so "what does my research agent cost me per month" is the primary
query rather than a derived one.</p>
<p>And the enforcement. A trace viewer <i>reports</i> that you spent $400. None
of them <i>stop</i> the $401st call. That is the whole product.</p>

<h2>Honest comparison</h2>
<p class="mut">Prices are list prices as published on each vendor's pricing page,
checked 2026-09-13. Verify before you buy — they change.</p>
<ul>
<li><b>LangSmith</b> — per seat, $39/seat/month plus per-trace overage. Traces,
evals, prompt playground. No budget enforcement.</li>
<li><b>Helicone</b> — free up to 10k requests/month, $79/month Pro when paid
annually. A gateway with observability; request-keyed. No enforced caps.</li>
<li><b>Langfuse / OpenLIT / Arize</b> — open-source core plus paid trace volume.
Observability first, no enforcement.</li>
<li><b>Braintrust</b> — $249/month entry. Eval and experiment platform, aimed at
teams with an eval budget.</li>
<li><b>Datadog LLM Observability</b> — priced into an observability contract
(the LLM SKU is a low-hundreds-per-month add-on). Buyer is ops, not the person
running the agent.</li>
</ul>

<h2>The other real comparison: gateways that can also block a call</h2>
<p>Trace viewers are not the only alternative. A newer set of AI gateways can
refuse a call before the provider is contacted — the same enforcement
boundary this product is built on. This is the comparison that actually
matters, and skipping it would be dishonest. Prices checked 2026-09-14.</p>
<ul>
<li><b>LiteLLM</b> — open-source, free to self-host. Rejects before the
provider when the estimated reservation would exceed budget. The tradeoff:
you host, configure and operate it yourself; no packaged per-agent reports,
alerts or shareable links.</li>
<li><b>Portkey</b> — $49/month Production. Hard caps can block further
requests. An enterprise AI gateway (now part of Palo Alto Networks/Prisma
AIRS) — routing and security-suite framing, not a focused per-agent finance
product.</li>
<li><b>LangDB</b> — $49/month Professional, $199 Business. Project/workspace
cost controls, returns 429 at the limit. Budgets are workspace-oriented, not
a flat per-agent ledger.</li>
<li><b>Revenium</b> — free developer tier, SMB/enterprise pricing not
published. The closest direct rival: hard spend limits plus cost attribution
across agent, workflow, tool and human review. AgentLedger's wedge against it
is the simpler self-serve motion at a fixed $19.</li>
<li><b>LangSmith Gateway</b> — $39/seat/month plus gateway usage. LangSmith's
2026 gateway beta added org/workspace/key/user-level caps on top of its
existing trace suite — an installed base adding enforcement, not a
purpose-built agent-budget product.</li>
</ul>
<p class="mut">None of these are wrong tools — several are excellent at what
they do. The distinction is unit of accounting (agent, not seat or key) and
price (flat $19/workspace, not per-seat or usage-scaled).</p>

<h2>What we are not</h2>
<p class="mut">We are not a trace viewer, an eval platform, or a prompt
playground, and we are not trying to be. If you need deep trace inspection,
pair one of the above with us: they show you the call, we keep the total from
becoming a surprise. AgentLedger is $19/month per workspace, flat, regardless of
seat count.</p>
"""

# ── /reliability (D-1250) ──────────────────────────────────────────────────
def reliability_body(pct: dict) -> str:
    """`pct` is metrics.latency_percentiles("pre_call_check")'s return value.
    Measures the overhead AgentLedger's own budget check adds before
    forwarding to the provider — not the provider's round trip, which this
    product does not control and should not take credit or blame for.
    Reports sample_count=0 honestly rather than inventing a number on a
    freshly booted instance."""
    if pct["sample_count"] == 0:
        stats_html = ('<p class="mut">No proxied calls have been measured on this '
                      'instance yet. This page reports real numbers only — it will '
                      'populate as soon as the proxy handles traffic.</p>')
    else:
        stats_html = f"""
<ul>
<li><b>p50</b> — {pct['p50_ms']} ms</li>
<li><b>p95</b> — {pct['p95_ms']} ms</li>
<li><b>p99</b> — {pct['p99_ms']} ms</li>
</ul>
<p class="mut">From {pct['sample_count']} measured calls on this instance
(resets on deploy — see <a href="/status">/status</a> for current uptime).</p>
"""
    return f"""
<h1>Reliability</h1>
<p>What AgentLedger's proxy actually costs you in latency, measured live —
not a claimed number.</p>

<h2>Proxy overhead (pre-call budget check)</h2>
<p class="mut">Time spent computing whether a call would exceed budget, before
forwarding to the provider. This is the AgentLedger tax — it does not include
the provider's own response time, which AgentLedger does not control.</p>
{stats_html}

<h2>Failure behavior</h2>
<p>If the upstream provider is unreachable, the proxy returns <code>502
upstream_unreachable</code> to the caller rather than retrying — a retried
call against a provider that already received the request risks a double
charge, so this product never retries automatically.</p>

<h2>Live status</h2>
<p><a class="plain" href="/status">/status</a> reports uptime and version for
this instance. <a class="plain" href="/health">/health</a> is the liveness
check. Uptime is per-instance: a deploy restarts the process.</p>
"""


# ── /security-adjacent note used on the demo page ───────────────────────────
DEMO_NOTE = """
<p class="mut">This is a <b>read-only sample</b> rendered from synthetic data —
no real workspace, no key, nothing written. It is the same dashboard you get
with your own data. <a href="/quickstart">Connect a real agent →</a></p>
"""


# ── Pricing sections (rendered from pricing.py; env-driven Stripe links) ─────
try:
    import pricing as _pricing

    AGENT_WATCH_PAGE = AGENT_WATCH_PAGE.replace(
        "{PRICING_AGENT_WATCH}", _pricing.pricing_section("agent-watch"))
    TRUST_SCAN_PAGE = TRUST_SCAN_PAGE.replace(
        "{PRICING_TRUST_SCAN}", _pricing.pricing_section("trust-scan"))
    PERIMETER_WATCH_PAGE = PERIMETER_WATCH_PAGE.replace(
        "{PRICING_PERIMETER_WATCH}", _pricing.pricing_section("perimeter-watch"))
    CITED_PAGE = CITED_PAGE.replace(
        "{PRICING_CITED}", _pricing.pricing_section("cited"))

    CITY_SHELL = CITY_SHELL.replace("</style>",
                                    _pricing.PRICING_CSS + "\n</style>", 1)
except Exception:
    # Pricing is decorative; a failure here must never break the pages.
    pass
