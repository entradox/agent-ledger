"""The AI Agent City umbrella site: one site, two readers.

Generated from the two-surfaces mockup by hidden_files/build_city_site.py —

re-run that script after mockup edits instead of hand-editing this file.

Human marketing surface (gold) + machine-readable terminal surface (green),

toggled per page via the Human | Agent segmented control.

"""
from __future__ import annotations

CSS = """  :root{
    --bg:#0a0c10; --panel:#11151c; --panel2:#171d27; --line:#232c3b;
    --txt:#f2f5f9; --mut:#9aa4b2; --dim:#5f6b7c;
    --human:#e8b93e; --agent:#7ee787; --agent-dim:#2d5a35;
    --mono:"SF Mono","Cascadia Code","JetBrains Mono",Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Segoe UI",Inter,Helvetica,Arial,sans-serif;
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--bg);color:var(--txt);font-family:var(--sans);line-height:1.6;font-size:16px}
    position:sticky;top:0;z-index:60;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
  nav.tabs{background:rgba(17,21,28,.92);backdrop-filter:blur(8px);border-bottom:1px solid var(--line);
    padding:0 20px;display:flex;gap:4px;overflow-x:auto;position:sticky;top:37px;z-index:59;align-items:center}
  nav.tabs a{background:none;text-decoration:none;border:none;color:var(--mut);font-family:var(--mono);font-size:13px;
    padding:14px 14px;cursor:pointer;border-bottom:2px solid transparent;white-space:nowrap}
  nav.tabs a:hover{color:var(--txt)}
  nav.tabs a.active{color:var(--human);border-bottom-color:var(--human)}
  .seg{margin-left:auto;display:flex;border:1px solid var(--line);border-radius:20px;overflow:hidden;flex-shrink:0}
  .seg button{padding:8px 16px;font-size:12px;border-bottom:none!important}
  .seg button.on-h{background:var(--human);color:#191104;font-weight:700}
  .seg button.on-a{background:var(--agent);color:#04120a;font-weight:700}
  .page{display:none;max-width:1080px;margin:0 auto;padding:56px 24px 80px}
  .page.active{display:block}
  .agent-surface{display:none}
  body.agent-view .human-surface{display:none!important}
  body.agent-view .agent-surface{display:block!important}
  body.agent-view nav.tabs a.active{color:var(--agent);border-bottom-color:var(--agent)}
  h1{font-size:52px;line-height:1.05;letter-spacing:-.025em;margin-bottom:18px;font-weight:800}
  h2{font-size:30px;letter-spacing:-.015em;margin:64px 0 18px;font-weight:750}
  h3{font-size:20px;margin:0 0 8px;font-weight:700}
  @media(max-width:760px){h1{font-size:36px}.hero-grid{grid-template-columns:1fr!important}}
  .kicker{font-family:var(--mono);font-size:12px;letter-spacing:.16em;color:var(--dim);text-transform:uppercase;margin-bottom:12px}
  .kicker .rh{color:var(--human)} .kicker .ra{color:var(--agent)}
  .lede{font-size:20px;color:var(--mut);max-width:680px;margin-bottom:30px}
  .lede b{color:var(--txt)}
  .cta-row{display:flex;gap:12px;flex-wrap:wrap;margin:30px 0;align-items:center}
  .btn{display:inline-block;padding:14px 26px;border-radius:10px;font-weight:700;font-size:15px;text-decoration:none;cursor:pointer;border:1px solid transparent}
  .btn-human{background:var(--human);color:#191104}
  .btn-agentb{background:transparent;color:var(--agent);border-color:var(--agent-dim);font-family:var(--mono);font-size:14px}
  .btn-ghost{background:transparent;color:var(--txt);border-color:var(--line)}
  .hero-grid{display:grid;grid-template-columns:1.05fr .95fr;gap:36px;align-items:center;margin-top:8px}
  /* live agent session terminal */
  .session{background:#07090d;border:1px solid var(--agent-dim);border-radius:14px;overflow:hidden;box-shadow:0 0 60px rgba(126,231,135,.06)}
  .session .shead{display:flex;align-items:center;gap:8px;padding:12px 16px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:12px;color:var(--mut)}
  .session .shead .dots{display:flex;gap:6px}
  .session .shead .dots i{width:10px;height:10px;border-radius:50%;background:#2a3342;display:block}
  .session .shead .live{margin-left:auto;color:var(--agent);display:flex;align-items:center;gap:6px}
  .session .shead .live i{width:8px;height:8px;border-radius:50%;background:var(--agent);animation:blink 1.6s infinite}
  @keyframes blink{0%,100%{opacity:1}50%{opacity:.25}}
  .session .sbody{padding:18px;font-family:var(--mono);font-size:13px}
  .step{display:flex;gap:12px;padding:10px 0;border-bottom:1px dashed #1c2330;align-items:flex-start}
  .step:last-child{border-bottom:none}
  .step .dot{width:10px;height:10px;border-radius:50%;background:#2a3342;margin-top:5px;flex-shrink:0}
  .step.run .dot{background:var(--human);animation:blink 1s infinite}
  .step.done .dot{background:var(--agent)}
  .step .t{color:var(--dim)} .step.run .t{color:var(--txt)} .step.done .t{color:var(--mut)}
  .step .cmd{color:var(--agent);display:block;margin-top:2px}
  .step .res{color:var(--dim);display:block}
  .step.done .res{color:#6f8a76}
  .step .res b{color:var(--agent);font-weight:600}
  /* rails strip */
  .rails{display:flex;gap:10px;flex-wrap:wrap;margin:30px 0;padding:18px 0;border-top:1px solid var(--line);border-bottom:1px solid var(--line)}
  .rails span{font-family:var(--mono);font-size:12px;color:var(--mut);border:1px solid var(--line);border-radius:8px;padding:8px 14px;background:var(--panel)}
  .rails span b{color:var(--txt)}
  .rails .rl{font-size:11px;color:var(--dim);border:none;background:none;padding:8px 4px;letter-spacing:.1em}
  /* numbered stack */
  .stacknum{font-family:var(--mono);font-size:12px;color:var(--dim);letter-spacing:.12em;margin-bottom:10px}
  .stacknum b{color:var(--human)}
  .card{background:var(--panel);border:1px solid var(--line);border-radius:14px;padding:28px;margin-bottom:18px}
  .card.hl{border-color:#5a4a1c}
  .card p{color:var(--mut);font-size:15px;margin-bottom:14px}
  .card p b{color:var(--txt)}
  .card .links{display:flex;gap:16px;flex-wrap:wrap;font-size:14px;margin-top:6px}
  .card .links a{color:var(--human);text-decoration:none;font-weight:600}
  .card .links a.ag{color:var(--agent);font-family:var(--mono);font-size:13px}
  .grid2{display:grid;grid-template-columns:1fr 1fr;gap:18px}
  .grid3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px}
  @media(max-width:760px){.grid2,.grid3{grid-template-columns:1fr}}
  pre{background:#07090d;border:1px solid var(--line);border-radius:10px;padding:18px;overflow-x:auto;
    font-family:var(--mono);font-size:13px;line-height:1.6;color:#c9d4e3;margin:14px 0;position:relative}
  pre .c{color:var(--dim)} pre .k{color:var(--agent)} pre .s{color:#a5d6ff}
  .copybtn{position:absolute;top:10px;right:10px;background:var(--panel2);border:1px solid var(--line);color:var(--mut);
    font-family:var(--mono);font-size:11px;padding:4px 10px;border-radius:6px;cursor:pointer}
  .thesis{display:grid;grid-template-columns:1fr 1fr 1fr;gap:16px;margin:34px 0}
  @media(max-width:760px){.thesis{grid-template-columns:1fr}}
  .thesis .t{background:var(--panel);border:1px solid var(--line);border-radius:12px;padding:22px}
  .thesis .t .n{font-family:var(--mono);color:var(--human);font-size:13px;margin-bottom:8px}
  .thesis .t h4{font-size:16px;margin-bottom:6px}
  .thesis .t p{font-size:14px;color:var(--mut)}
  .pull{border-left:3px solid var(--human);padding:6px 0 6px 20px;margin:30px 0;font-size:21px;color:var(--txt);font-weight:600;max-width:660px}
  ul.feat{list-style:none;margin:14px 0}
  ul.feat li{padding:9px 0 9px 28px;border-bottom:1px solid var(--line);font-size:15px;color:var(--mut);position:relative}
  ul.feat li:before{content:"→";position:absolute;left:4px;color:var(--human)}
  ul.feat li b{color:var(--txt)}
  ul.feat li:last-child{border-bottom:none}
  table.cmp{width:100%;border-collapse:collapse;font-size:14px;margin-top:16px}
  table.cmp th,table.cmp td{border:1px solid var(--line);padding:12px 14px;text-align:left;vertical-align:top}
  table.cmp th{background:var(--panel);font-family:var(--mono);font-size:13px}
  table.cmp td.y{color:var(--agent)} table.cmp td.n{color:var(--mut)} table.cmp td.part{color:var(--human)}
  table.cmp .note{font-size:12px;color:var(--dim);display:block;margin-top:6px}
  .us{color:var(--human);font-weight:700}
  .chlog{border-left:2px solid var(--line);margin:20px 0 0 8px;padding-left:24px}
  .chlog .e{margin-bottom:22px;position:relative}
  .chlog .e:before{content:"";position:absolute;left:-31px;top:6px;width:10px;height:10px;border-radius:50%;background:var(--human)}
  .chlog .d{font-family:var(--mono);font-size:12px;color:var(--dim)}
  .chlog .t{font-size:15px;margin-top:2px;color:var(--mut)}
  .chlog .t b{color:var(--txt)}
  .ver{font-family:var(--mono);font-size:12px;color:var(--dim);margin-left:10px}
  .illus{font-family:var(--mono);font-size:11px;color:var(--dim);margin-top:10px}
  footer.site{border-top:1px solid var(--line);margin-top:72px;padding:28px 0;color:var(--dim);font-size:13px;
    display:flex;gap:18px;flex-wrap:wrap;justify-content:space-between}
  footer.site a{color:var(--mut);text-decoration:none;margin-right:16px;font-family:var(--mono);font-size:12px}
  footer.site a:hover{color:var(--txt)}
  /* agent surface */
  .agent-surface .term{background:#07090d;border:1px solid var(--agent-dim);border-radius:14px;overflow:hidden}
  .agent-surface .term .thead{padding:12px 18px;border-bottom:1px solid var(--line);font-family:var(--mono);font-size:12px;color:var(--agent)}
  .agent-surface .term pre{border:none;margin:0;border-radius:0;background:transparent}
  .agent-note{font-family:var(--mono);font-size:12px;color:var(--dim);margin:14px 0;max-width:720px}
  .agent-note b{color:var(--agent)}"""

TOGGLE_JS = """<script>
function setView(v){
  document.body.classList.toggle('agent-view', v==='agent');
  document.getElementById('segH').className = v==='human' ? 'on-h' : '';
  document.getElementById('segA').className = v==='agent' ? 'on-a' : '';
  window.scrollTo({top:0});
}
function copyPre(btn){
  const pre=btn.parentElement;
  const text=Array.from(pre.childNodes).filter(n=>n!==btn).map(n=>n.textContent).join('');
  navigator.clipboard.writeText(text.trim()).then(()=>{btn.textContent='copied';setTimeout(()=>btn.textContent='copy',1200);});
}
/* live agent session animation */
(function(){
  const steps=document.querySelectorAll('#session .step');
  if(!steps.length) return;
  let i=0;
  function cycle(){
    steps.forEach(s=>s.classList.remove('run','done'));
    let n=0;
    const tick=setInterval(()=>{
      if(n>0){steps[n-1].classList.remove('run');steps[n-1].classList.add('done');}
      if(n>=steps.length){clearInterval(tick);setTimeout(cycle,2600);return;}
      steps[n].classList.add('run'); n++;
    },1400);
  }
  cycle();
})();
</script>"""

SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="__DESC__">
<style>
""" + CSS + """
</style></head>
<body>
__NAV__
<div class="wrap">
__BODY__
</div>
""" + TOGGLE_JS + """
</body></html>"""

TABS = [
    ("/", "/", None),
    ("/products", "/products", None),
    ("/developers", "/developers", None),
    ("/compare", "/compare", None),
    ("/changelog", "/changelog", None),
    ("/spec", "/spec — founder gaps", 'color:var(--agent)'),
]


def nav(active: str) -> str:
    parts = ['<nav class="tabs" id="tabs">']
    for path, label, style in TABS:
        cls = ' class="active"' if path == active else ""
        st = f' style="{style}"' if style else ""
        parts.append(f'<a href="{path}"{cls}{st}>{label}</a>')
    parts.append('<div class="seg" id="seg">'
                 '<button id="segH" class="on-h" onclick="setView(\'human\')">Human</button>'
                 '<button id="segA" onclick="setView(\'agent\')">Agent</button>'
                 "</div></nav>")
    return "".join(parts)


def render(active: str, title: str, desc: str, body: str) -> str:
    html = SHELL
    html = html.replace("__TITLE__", title)
    html = html.replace("__DESC__", desc)
    html = html.replace("__NAV__", nav(active))
    html = html.replace("__BODY__", body)
    return html

PAGE_HOME = """<div class="human-surface">
  <div class="kicker"><span class="rh">// human-readable</span> · AI Agent City</div>
  <div class="hero-grid">
    <div>
      <h1>Agents are economic<br>actors now.</h1>
      <p class="lede">They spend money. They call your APIs. They represent businesses. <b>AI Agent City is the operations layer they run on</b> — budgets, monitoring, security, trust, and discovery. Live software, MCP everywhere; REST + CLI on AgentLedger.</p>
      <div class="cta-row">
        <a class="btn btn-human" href="/products">Explore the stack</a>
        <a class="btn btn-ghost" href="/start">Start free</a>
      </div>
    </div>
    <div>
      <div class="session" id="session">
        <div class="shead"><div class="dots"><i></i><i></i><i></i></div><span>agent session — live</span><span class="live"><i></i>LIVE</span></div>
        <div class="sbody">
          <div class="step" data-s="0"><span class="dot"></span><div><span class="t">discover</span><span class="cmd">GET /.well-known/x402.json</span><span class="res"><b>200</b> · mainnet: eip155:8453 · accepts USDC</span></div></div>
          <div class="step" data-s="1"><span class="dot"></span><div><span class="t">connect</span><span class="cmd">mcp add agent-ledger https://aiagentscity.com/mcp/</span><span class="res"><b>9 tools</b> registered · ledger_track · ledger_set_budget …</span></div></div>
          <div class="step" data-s="2"><span class="dot"></span><div><span class="t">transact</span><span class="cmd">POST /v1/billing/x402 · X-PAYMENT: &lt;signed&gt;</span><span class="res"><b>200</b> · 24h Pro activated · $0.01 USDC settled</span></div></div>
          <div class="step" data-s="3"><span class="dot"></span><div><span class="t">enforce</span><span class="cmd">ledger_set_budget {agent: "researcher", monthly: $50}</span><span class="res">cap armed · over-budget calls → <b>402</b> before provider contact</span></div></div>
        </div>
      </div>
      <p class="illus">▲ this is the agent-native surface, playing live next to the human pitch — the whole site works like this</p>
    </div>
  </div>

  <div class="rails">
    <span class="rl">RAILS</span><span><b>MCP</b> · tool protocol</span><span><b>x402</b> · payment protocol</span><span><b>Base</b> · eip155:8453</span><span><b>USDC</b> · settlement</span><span><b>llms.txt</b> · discovery</span><span><b>A2A</b> · ecosystem</span>
  </div>

  <div class="kicker"><span class="rh">// human-readable</span> · why now</div>
  <div class="thesis">
    <div class="t"><div class="n">01</div><h4>Agents hold wallets</h4><p>Machine-to-machine payments settle on-chain for fractions of a cent. The moment agents spend, someone has to set the budget.</p></div>
    <div class="t"><div class="n">02</div><h4>Agents call your APIs</h4><p>Non-human traffic already hits production endpoints, and most teams can't see it. You can't secure or bill what you can't attribute.</p></div>
    <div class="t"><div class="n">03</div><h4>AI answers are the new search</h4><p>Customers ask ChatGPT and Claude instead of Googling. If the models don't recommend you — with receipts — you're invisible.</p></div>
  </div>

  <div class="kicker"><span class="rh">// human-readable</span> · <span class="ra">// machine-readable</span> · the stack</div>
  <div class="pull">"Every economic actor needs five things: a budget, a monitor, a perimeter, a reputation, and a way to be found. We're building all five — for agents."</div>

  <div class="card hl">
    <div class="stacknum"><b>01</b> — CONTROL SPEND · FLAGSHIP <span class="ver">v0.4.1 · 9 tools</span></div>
    <h3>AgentLedger — spending limits for AI agents</h3>
    <p>The call that would break the budget <b>never reaches the provider</b>. Per-agent P&amp;L, enforced dollar and token caps, anomaly alerts — and the agent itself buys Pro for $0.01 with no human involved.</p>
    <div class="links"><a href="/products">How it works →</a><a href="/demo">Live demo →</a><a class="ag" href="/developers">$ mcp add agent-ledger →</a></div>
  </div>
  <div class="grid2">
    <div class="card"><div class="stacknum"><b>02</b> — MONITOR <span class="ver">v1.30.0 · 6 tools</span></div><h3>Agent Watch</h3><p>Monitoring for the agent economy. Know the moment a new agent touches your API.</p><div class="links"><a href="/products">Explore →</a><a class="ag" href="/developers">$ mcp add agent-watch →</a></div></div>
    <div class="card"><div class="stacknum"><b>03</b> — SECURE <span class="ver">v1.30.0 · 4 tools</span></div><h3>Perimeter Watch</h3><p>Dangling DNS, expiring certs, lookalike domains — caught before they're incidents.</p><div class="links"><a href="/products">Explore →</a><a class="ag" href="/developers">$ mcp add perimeter-watch →</a></div></div>
    <div class="card"><div class="stacknum"><b>04</b> — TRUST <span class="ver">v4.0.3 · 2 tools · MCP</span></div><h3>TrustScan</h3><p>Scan before you trust. Know who you're dealing with before your agent does.</p><div class="links"><a href="/products">Explore →</a></div></div>
    <div class="card"><div class="stacknum"><b>05</b> — BE FOUND <span class="ver">v1.30.0 · 7 tools</span></div><h3>Cited</h3><p>Does AI recommend your practice? Instant scan, verbatim evidence.</p><div class="links"><a href="/products">Explore →</a><a class="ag" href="/developers">$ mcp add cited →</a></div></div>
  </div>

  <div class="kicker"><span class="rh">// human-readable</span> · proof</div>
  <h2 style="margin-top:12px">Live, not slides</h2>
  <div class="grid3">
    <div class="card"><h3>$0.01, zero clicks</h3><p>An agent bought 24h of AgentLedger Pro over x402 on Base mainnet — real USDC, no human in the loop.</p></div>
    <div class="card"><h3>7.4× more accurate</h3><p>Flat-rate token pricing was 7.4× wrong on a real session. AgentLedger prices cache-aware.</p></div>
    <div class="card"><h3>402, not a dashboard</h3><p>Trace viewers report after you've paid. AgentLedger refuses the $401st call <b>before the provider sees it</b>.</p></div>
  </div>
  <div class="cta-row"><a class="btn btn-ghost" href="/compare">AgentLedger vs trace viewers →</a><a class="btn btn-agentb" href="/developers">I'm an agent — take me to /developers →</a></div>

  <footer class="site">
    <div><a href="/products">/products</a><a href="/developers">/developers</a><a href="/compare">/compare</a><a href="/changelog">/changelog</a><a href="/llms.txt">/llms.txt</a><a href="/status">status</a><a href="/security">security</a></div>
    <div>AI Agent City · one site, two readers</div>
  </footer>
</div>

<div class="agent-surface">
  <div class="kicker"><span class="ra">// machine-readable</span> · what an agent sees on /</div>
  <p class="agent-note">Flip the <b>Human | Agent</b> toggle in the nav on any page. This is the agent-native surface: the same content, structured for machines — the live equivalent of <b>llms.txt</b>. No marketing prose, no missing endpoints.</p>
  <div class="term"><div class="thead">$ curl https://aiagentscity.com/llms.txt</div><pre>
<span class="k"># AI Agent City</span> — operations layer for the agent economy
<span class="c"># Products (MCP across the board; REST + CLI on AgentLedger)</span>
- <span class="k">AgentLedger</span>: per-agent spend caps, 402 enforcement pre-provider.
  MCP: /mcp/ (9 tools) · REST: /v1/track /v1/budget /v1/report
  Pricing: free ≤3 agents · Pro $19/mo flat · x402: $0.01 = 24h Pro (Base eip155:8453, USDC)
  Purchase (no human): POST /v1/billing/x402 + X-PAYMENT header
- <span class="k">Agent Watch</span>: agent-economy monitoring. MCP: /mcp/agent-watch (6 tools)
- <span class="k">Perimeter Watch</span>: external perimeter scans. MCP: /mcp/perimeter-watch (4 tools)
- <span class="k">TrustScan</span>: pre-transaction counterparty scans. MCP (2 tools · v4.0.3). Access: request
- <span class="k">Cited</span>: AI-visibility scans, verbatim evidence. MCP: /mcp/cited (7 tools)
<span class="c"># Machine entry points</span>
  /.well-known/x402.json · /skill.md · /openapi.json · /server.json · /status
</pre></div>
  <p class="agent-note">Every page on the live site carries this dual surface. Agents never scrape marketing copy — they read the contract.</p>
  <footer class="site"><div><a href="/products">/products</a><a href="/developers">/developers</a></div><div>AI Agent City</div></footer>
</div>"""

PAGE_PRODUCTS = """<div class="human-surface">
  <div class="kicker"><span class="rh">// human-readable</span> · aiagentscity.com/products</div>
  <h1>One stack.<br>Five products.</h1>
  <p class="lede">Each solves one operational problem end to end. Each is <b>live, independently usable, and agent-callable</b> — MCP everywhere; REST + CLI on AgentLedger.</p>

  <div class="card hl">
    <div class="stacknum"><b>01</b> — CONTROL SPEND · FLAGSHIP <span class="ver">v0.4.1 · 9 tools</span></div>
    <h3>AgentLedger — spending limits for AI agents</h3>
    <p><b>Your agents spend money. Give each one a spending limit.</b> Meters every call, shows a live P&amp;L per agent, and refuses the call that would cross the budget — before the provider is contacted.</p>
    <ul class="feat">
      <li><b>Pre-provider enforcement</b> — the proxy estimates max cost and returns <b>402</b>. The provider never sees the request; the money is never spent.</li>
      <li><b>Cache-aware pricing</b> — real tokens × real model rates. Flat-rate was 7.4× wrong on a real session.</li>
      <li><b>Agents buy their own Pro</b> — $0.01 x402 on Base mainnet, 24h, zero human clicks.</li>
      <li><b>Push alerts + shareable reports</b> — webhooks with retries; signed, expiring report links.</li>
    </ul>
    <pre><button class="copybtn" onclick="copyPre(this)">copy</button>HTTP/1.1 <span class="k">402</span> Payment Required
{<span class="s">"error"</span>:{<span class="s">"type"</span>:<span class="s">"budget_exceeded"</span>, <span class="s">"message"</span>:<span class="s">"blocked before the provider"</span>}}</pre>
    <p style="font-size:14px"><b style="color:var(--txt)">Free:</b> 3 agents, every rail, enforced caps. <b style="color:var(--txt)">Pro: $19/mo flat</b> — unlimited agents, never per seat.</p>
    <div class="cta-row"><a class="btn btn-human" href="/start">Get a workspace — no signup, no card</a><a class="btn btn-agentb" href="/developers">$ mcp add agent-ledger →</a></div>
  </div>

  <div class="card">
    <div class="stacknum"><b>02</b> — MONITOR <span class="ver">v1.30.0 · 6 MCP tools live</span></div>
    <h3>Agent Watch — monitoring for the agent economy</h3>
    <p><b>Non-human traffic is hitting your APIs and you can't see it.</b> What changed this week, alerts the moment a new agent calls your endpoints, anomaly summaries — before they become incidents or invoices.</p>
    <pre><button class="copybtn" onclick="copyPre(this)">copy</button><span class="c">"Alert me when a new agent calls our API, with a weekly activity summary."</span></pre>
    <div class="cta-row"><a class="btn btn-human" href="/agent-watch">Start monitoring</a><a class="btn btn-agentb" href="/developers">$ mcp add agent-watch →</a></div>
  </div>

  <div class="card">
    <div class="stacknum"><b>03</b> — SECURE <span class="ver">v1.30.0 · 4 MCP tools live</span></div>
    <h3>Perimeter Watch — passive external-perimeter monitoring for web agencies</h3>
    <p><b>Attackers view your clients' domains from the outside. So do we.</b> Dangling DNS, expiring certs, lookalike domains — scanned passively, briefed weekly.</p>
    <pre><button class="copybtn" onclick="copyPre(this)">copy</button><span class="c">"Scan example.com for dangling DNS and cert expiry, then check lookalikes."</span></pre>
    <div class="cta-row"><a class="btn btn-human" href="/perimeter-watch">Scan a domain</a><a class="btn btn-agentb" href="/developers">$ mcp add perimeter-watch →</a></div>
  </div>

  <div class="card">
    <div class="stacknum"><b>04</b> — TRUST <span class="ver">v4.0.3 · 2 tools · MCP</span></div>
    <h3>TrustScan — scan before you trust</h3>
    <p><b>Your agent is about to do business with a stranger.</b> TrustScan answers the question every agent should ask before money, data, or access changes hands: <i>who am I dealing with?</i> A verdict with evidence — not a black-box score — callable over MCP today <span class="ver">v4.0.3 · 2 tools live · dedicated connect page launching soon</span>, built to sit in front of any agent transaction.</p>
    <div class="cta-row"><a class="btn btn-human" href="/trust-scan">Request access</a></div>
  </div>

  <div class="card">
    <div class="stacknum"><b>05</b> — BE FOUND <span class="ver">v1.30.0 · 7 MCP tools live</span></div>
    <h3>Cited — does AI recommend your practice?</h3>
    <p><b>Your customers stopped Googling. They ask AI.</b> Instant scan of what the AI engines say about your business — with verbatim evidence. Free.</p>
    <pre><button class="copybtn" onclick="copyPre(this)">copy</button><span class="c">"Scan Gentry Dentistry of Suwanee for AI visibility — verbatim quotes."</span></pre>
    <div class="cta-row"><a class="btn btn-human" href="/cited">Run your free scan</a><a class="btn btn-agentb" href="/developers">$ mcp add cited →</a></div>
  </div>

  <div class="pull">Five products, one thesis: the agent economy needs operations. We're building the boring infrastructure that makes the exciting future possible.</div>
  <footer class="site"><div><a href="/">← /</a><a href="/developers">/developers</a></div><div>AI Agent City</div></footer>
</div>

<div class="agent-surface">
  <div class="kicker"><span class="ra">// machine-readable</span> · what an agent sees on /products</div>
  <div class="term"><div class="thead">$ agent-view /products</div><pre>
<span class="k">products:</span>
  - id: agent-ledger · mcp: /mcp/ (9 tools) · rest: /v1/*
    pricing: {free: "≤3 agents", pro: "$19/mo flat"} · x402: "$0.01 = 24h Pro"
  - id: agent-watch · mcp: /mcp/agent-watch (6 tools)
  - id: perimeter-watch · mcp: /mcp/perimeter-watch (4 tools)
  - id: trustscan · access: request · protocols: [mcp]
  - id: cited · mcp: /mcp/cited (7 tools) · free_scan: true
<span class="k">capabilities:</span> [spend-caps, monitoring, perimeter-scan, trust-scan, ai-visibility]
<span class="k">auth:</span> workspace_key (human) | X-PAYMENT x402 (agent, no human)
</pre></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>"""

PAGE_DEVELOPERS = """<div class="human-surface">
  <div class="kicker"><span class="rh">// human-readable</span> · <span class="ra">// machine-readable</span> · aiagentscity.com/developers</div>
  <h1>We sell to agents<br>directly.</h1>
  <p class="lede">Not through humans. Machine-readable docs, self-serve keys, self-serve billing. <b>If you're an agent, everything below is your sales rep, your docs, and your checkout.</b> Humans: the quickstarts are copy-paste.</p>

  <div class="grid2">
    <div class="card"><h3>llms.txt</h3><p>The machine-readable contract for the whole city: products, endpoints, auth, pricing. Agents read this first.</p><div class="links"><a href="/llms.txt">Read rendered →</a><a class="ag" href="/llms.txt">raw →</a></div></div>
    <div class="card"><h3>skill.md</h3><p>Drop-in agent skill. Track spend, set budgets, pull reports, buy Pro — one file, zero config beyond a key.</p><div class="links"><a href="/skill.md">Read →</a><a class="ag" href="/skill.md">raw →</a></div></div>
  </div>

  <h2>The MCP catalog</h2>
  <table class="cmp">
    <tr><th>Server</th><th>What your agent gets</th><th>Connect</th></tr>
    <tr><td><b>agent-ledger</b> <span class="ver">9 tools</span></td><td style="color:var(--mut)">Track spend, set budgets, pull P&amp;L, manage alerts</td><td><code style="font-family:var(--mono);font-size:12px">…/mcp/</code></td></tr>
    <tr><td><b>agent-watch</b> <span class="ver">6 tools</span></td><td style="color:var(--mut)">Activity briefs, new-agent alerts, anomaly summaries</td><td><code style="font-family:var(--mono);font-size:12px">…/mcp/agent-watch</code></td></tr>
    <tr><td><b>perimeter-watch</b> <span class="ver">4 tools</span></td><td style="color:var(--mut)">Dangling-DNS scans, cert watch, lookalike checks</td><td><code style="font-family:var(--mono);font-size:12px">…/mcp/perimeter-watch</code></td></tr>
    <tr><td><b>cited</b> <span class="ver">7 tools</span></td><td style="color:var(--mut)">AI-visibility scans with verbatim evidence</td><td><code style="font-family:var(--mono);font-size:12px">…/mcp/cited</code></td></tr>
    <tr><td><b>trustscan</b> <span class="ver">2 tools · v4.0.3</span></td><td style="color:var(--mut)">Pre-transaction counterparty scans</td><td style="color:var(--mut)">MCP · connect page launching soon</td></tr>
  </table>
  <pre><button class="copybtn" onclick="copyPre(this)">copy</button><span class="c"># one command and your agent is connected</span>
claude mcp add --transport http agent-ledger https://aiagentscity.com/mcp/</pre>

  <h2>Checkout without humans</h2>
  <div class="card" style="border-color:var(--agent-dim)">
    <h3>$0.01. Base mainnet. Zero clicks.</h3>
    <p>An agent with a wallet buys 24h of AgentLedger Pro — unlimited agents — on the workspace its wallet resolves to. Real USDC on <b>eip155:8453</b>. No signup flow, no card form, no human.</p>
    <pre><button class="copybtn" onclick="copyPre(this)">copy</button>POST /v1/billing/x402
Header: <span class="s">X-PAYMENT: &lt;signed-payload&gt;</span>
<span class="c"># → 24h Pro. That's the whole checkout.</span></pre>
    <div class="links"><a class="ag" href="/.well-known/x402.json">/.well-known/x402.json →</a><a href="/agent-ledger">Buying guide →</a></div>
  </div>

  <h2>Humans start here</h2>
  <div class="card">
    <h3>AgentLedger in one line</h3>
    <pre><button class="copybtn" onclick="copyPre(this)">copy</button>pip install <span class="s">"aiagentscity-ledger[wrapper]"</span>
client = agentledger.<span class="k">wrap</span>(OpenAI(), agent_id=<span class="s">"my-agent"</span>, agent_secret=<span class="s">"as_..."</span>)</pre>
    <p>One press mints the workspace key — no signup, no login, no card. Shown once, on the spot.</p>
    <div class="cta-row"><a class="btn btn-human" href="/start">Get a workspace</a></div>
  </div>
  <footer class="site"><div><a href="/">← /</a><a href="/openapi.json">openapi.json</a><a href="/server.json">server.json</a><a href="/status">/status</a></div><div>AI Agent City</div></footer>
</div>

<div class="agent-surface">
  <div class="kicker"><span class="ra">// machine-readable</span> · what an agent sees on /developers</div>
  <div class="term"><div class="thead">$ agent-view /developers</div><pre>
<span class="k">discovery:</span> /.well-known/x402.json · /llms.txt · /skill.md
<span class="k">mcp:</span>
  agent-ledger: https://aiagentscity.com/mcp/  <span class="c"># 9 tools</span>
  agent-watch: .../mcp/agent-watch            <span class="c"># 6 tools</span>
  perimeter-watch: .../mcp/perimeter-watch    <span class="c"># 4 tools</span>
  cited: .../mcp/cited                        <span class="c"># 7 tools</span>
<span class="k">purchase:</span>
  POST /v1/billing/x402 · header X-PAYMENT=&lt;signed&gt;
  price: $0.01 USDC · chain: eip155:8453 · grants: 24h Pro
<span class="k">rest:</span> /openapi.json · /server.json · AL-API-Version: 2026-09-01 (required on writes)
</pre></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>"""

PAGE_COMPARE = """<div class="human-surface">
  <div class="kicker"><span class="rh">// human-readable</span> · aiagentscity.com/compare · prices verified 2026-09-18</div>
  <h1>Built to enforce.<br>Not to report.</h1>
  <p class="lede">Trace viewers are excellent at answering <i>"what did my agent do?"</i> — after you've paid for it. AgentLedger answers a different question: <i>"stop the call that breaks the budget."</i> Here's the honest map, including where the competition wins.</p>
  <table class="cmp">
    <tr><th></th><th><span class="us">AgentLedger</span></th><th>LangSmith</th><th>Helicone</th><th>Braintrust</th><th>OpenRouter</th></tr>
    <tr><td><b>Stops the overspend</b></td><td class="y"><b>Yes — 402 before provider contact</b></td><td class="part">Yes — gateway spend policies, 402<span class="note">their gateway traffic only</span></td><td class="part">Spend-based rate limits<span class="note">throttles, not budgets</span></td><td class="n">No — cost regression tracking</td><td class="part">Per-key spend limits<span class="note">OpenRouter-routed traffic only</span></td></tr>
    <tr><td><b>Budgets belong to the agent</b></td><td class="y"><b>Yes — keyed to agent_id</b></td><td class="n">Org / key / user</td><td class="n">No</td><td class="n">No</td><td class="n">API keys</td></tr>
    <tr><td><b>Agent buys itself</b></td><td class="y"><b>$0.01 x402, no human</b></td><td class="n">No</td><td class="n">No</td><td class="n">No</td><td class="n">No</td></tr>
    <tr><td><b>Trace debugging</b></td><td class="n">No — by design</td><td class="y">Best in class</td><td class="y">Yes</td><td class="y">Eval-first</td><td class="n">No</td></tr>
    <tr><td><b>Price</b></td><td class="y"><b>$19/mo flat</b></td><td class="n">$39/seat/mo</td><td class="n">$79/mo</td><td class="n">$249/mo</td><td class="n">usage-based</td></tr>
  </table>
  <div class="pull" style="font-size:19px">If you want to <i>understand</i> your spend, buy a trace viewer — LangSmith's is superb. If you want to <i>control</i> it, per agent, with the agent itself as the customer — that's us.</div>
  <div class="card"><h3>Also in the space</h3><p class="mut"><b>LiteLLM</b>, <b>Portkey</b>, <b>LangDB</b> — gateway proxies with per-key spend controls, scoped to the traffic routed through them. <b>Revenium</b> — API metering for monetization. <b>Langfuse</b> — open-source trace observability. Gateway- and trace-layer tools see their own layer; AgentLedger enforces per-agent budgets with a 402 before any provider is contacted, and the agent itself can buy Pro.</p></div>
  <div class="cta-row"><a class="btn btn-human" href="/demo">Try the enforcement live — no signup</a></div>
  <footer class="site"><div><a href="/">← /</a><a href="/products">/products</a></div><div>AI Agent City</div></footer>
</div>

<div class="agent-surface">
  <div class="kicker"><span class="ra">// machine-readable</span> · what an agent sees on /compare</div>
  <div class="term"><div class="thead">$ agent-view /compare</div><pre>
<span class="k">verdict:</span> "If you want to understand spend, buy a trace viewer.
         If you want to control it per-agent, with the agent as customer: us."
<span class="k">matrix:</span>
  enforcement:     {agentledger: "402 pre-provider", langsmith: "402 gateway-only",
                    helicone: "spend rate-limits", braintrust: "none", openrouter: "per-key spend limits"}
  budget_unit:     {agentledger: "agent_id", others: "org/key/user or none"}
  also_in_space:  {litellm: "gateway spend controls", portkey: "gateway spend controls",
                   langdb: "gateway spend controls", revenium: "api metering",
                   langfuse: "trace observability"}
  agent_purchase:  {agentledger: "$0.01 x402", others: "none"}
  price:           {agentledger: "$19/mo flat", langsmith: "$39/seat",
                    helicone: "$79/mo", braintrust: "$249/mo", openrouter: "usage"}
  prices_verified: "2026-09-18"
</pre></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>"""

PAGE_CHANGELOG = """<div class="human-surface">
  <div class="kicker"><span class="rh">// human-readable</span> · aiagentscity.com/changelog</div>
  <h1>Shipping fast.</h1>
  <p class="lede">Velocity is the pitch. Every ship, dated — the proof the stack is alive.</p>
  <div class="chlog">
    <div class="e"><div class="d">2026-09-16</div><div class="t"><b>x402 live on Base mainnet.</b> $0.01 USDC → 24h of AgentLedger Pro. Agents buy with zero human clicks — the first purchase completed end-to-end. <span class="ver">agent-ledger</span></div></div>
    <div class="e"><div class="d">2026-09-16</div><div class="t"><b>Four fixes from live testing.</b> Real <span class="ver">agent_secret="as_…"</span> format in the docs, SDK model IDs auto-priced as aliases, webhooks section on the dashboard, typed <span class="ver">payment_required</span> errors on the x402 endpoint. <span class="ver">agent-ledger</span></div></div>
    <div class="e"><div class="d">2026-09-16</div><div class="t"><b>Per-product releases.</b> AgentLedger <span class="ver">v0.4.1</span> (9 MCP tools) · Agent Watch <span class="ver">v1.30.0</span> (6) · Perimeter Watch <span class="ver">v1.30.0</span> (4) · Cited <span class="ver">v1.30.0</span> (7) · TrustScan <span class="ver">v4.0.3</span> (2). 28 MCP tools live across five products. <span class="ver">platform</span></div></div>
  </div>
  <div class="cta-row"><a class="btn btn-ghost" href="/status">/status — live system status →</a></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>

<div class="agent-surface">
  <div class="kicker"><span class="ra">// machine-readable</span> · what an agent sees on /changelog</div>
  <div class="term"><div class="thead">$ agent-view /changelog</div><pre>
<span class="k">releases:</span>
  - 2026-09-16: x402 live on eip155:8453 ($0.01 USDC = 24h Pro, zero-click)
  - 2026-09-16: agent-ledger fixes (agent_secret format, model alias pricing,
    dashboard webhooks, typed payment_required errors)
  - 2026-09-16: per-product releases (28 MCP tools: ledger 9, watch 6, perimeter 4, cited 7, trustscan 2)
<span class="k">feed:</span> /changelog (this page) · /status (live)
</pre></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>"""

PAGE_SPEC = """<div class="human-surface">
  <div class="kicker"><span class="ra">// founder spec</span> · read as a founder, written as a build list</div>
  <h1>What we're missing.</h1>
  <p class="lede">I read <b>OpenRouter</b>, <b>TypeSafe AI</b>, and <b>Skyfire</b> the way a founder reads competition: not for ideas to copy, but for <b>table stakes we're not meeting</b>. Everything below is something they ship that we don't — written as a spec, prioritized. P0 = this week. P1 = this month. P2 = next.</p>

  <div class="kicker"><span class="rh">// human-readable</span> · site-wide gaps</div>

  <div class="card">
    <div class="stacknum"><b>P0</b> · they lead with proof, we lead with prose</div>
    <h3>Metrics-with-receipts hero</h3>
    <p><b>TypeSafe</b> opens with "193.6x faster, 444.6x cheaper" <i>with a (proof) link</i> and a side-by-side video. <b>OpenRouter</b> opens with 400T+ tokens, 10M+ users. We open with a paragraph. <b>Spec:</b> the "Live, not slides" strip becomes three metrics, each linking its receipt — x402 settlement → BaseScan tx hash; 402 enforcement → dated live-test output; 7.4× pricing → the real session data. No claim without a receipt link.</p>
  </div>

  <div class="card">
    <div class="stacknum"><b>P0</b> · they show who's built on them, we show nobody</div>
    <h3>"Built on AI Agent City" — the agent wall</h3>
    <p><b>OpenRouter</b> ships "Featured Agents": 250k+ apps, 4.2M users — Replit, Kilo Code, and notably <b>Hermes Agent</b>. Our own agent is featured on <i>their</i> wall and absent from ours. <b>Spec:</b> an ecosystem wall on the homepage — start with agents we run ourselves (Hermes), then open submissions: name, what it does, which products it uses. For an agent-native company this wall <i>is</i> the social proof.</p>
  </div>

  <div class="card">
    <div class="stacknum"><b>P0</b> · their onboarding is 3 steps on the homepage, ours is a page away</div>
    <h3>Homepage onboarding strip</h3>
    <p><b>OpenRouter</b>: Signup → Buy credits → Get API key, right on the homepage with a masked key visual. <b>Spec:</b> homepage strip — <b>1.</b> Get a workspace key (one press, shown once) → <b>2.</b> Connect via MCP (copy-paste) → <b>3.</b> Set a cap (one number). Each step copy-pasteable in place.</p>
  </div>

  <div class="card">
    <div class="stacknum"><b>P1</b> · they rank the ecosystem, we hide our data</div>
    <h3>The Agent Economy Index</h3>
    <p><b>OpenRouter's</b> model rankings — tokens per model with weekly trends — are a destination in themselves. Nobody ranks the <i>agent</i> economy. <b>Spec:</b> a public, anonymized index at /index — agents tracked, USDC settled via x402, median cost per 1k agent calls, week-over-week trends. Updated daily. This is the page only we can build, and it markets every product at once.</p>
  </div>

  <div class="card">
    <div class="stacknum"><b>P1</b> · they sell credits to humans, we only sell to agents</div>
    <h3>Human billing page</h3>
    <p><b>OpenRouter</b>: buy credits, credits work everywhere. Our x402 rail sells to agents beautifully; humans get an API call to a Stripe checkout. <b>Spec:</b> /billing — card checkout for Pro ($19/mo), invoices, credit balance, cancel in one click. The agent rail stays; the human rail stops being embarrassing.</p>
  </div>

  <div class="card">
    <div class="stacknum"><b>P1</b> · their keys are managed, ours are shown once and prayed over</div>
    <h3>Key management UI</h3>
    <p><b>OpenRouter</b> shows masked keys with copy, per-key limits and labels. Our workspace key is shown once at /start and pasted into the dashboard from memory. <b>Spec:</b> dashboard → Keys: list, label, rotate, revoke. Losing a key stops being a support ticket.</p>
  </div>

  <div class="card">
    <div class="stacknum"><b>P1</b> · they answer skeptics, we hope nobody asks</div>
    <h3>Skeptic-grade FAQ per product</h3>
    <p><b>TypeSafe's</b> FAQ is a masterclass: "Are these prices temporary?", "Can Jev still get things wrong?", "How is this different from JSON mode?" — every objection a buyer has, answered before it's asked. <b>Spec:</b> a real FAQ on /agent-ledger and each product page: "What if my agent bypasses the proxy?" (answered: then it's not enforced — we say so), "Is the $0.01 x402 price subsidized?", "What do you store about my prompts?" (nothing — and here's the test that proves it).</p>
  </div>

  <div class="grid2">
    <div class="card">
      <div class="stacknum"><b>P1</b> · docs hub</div>
      <h3>/docs, not just llms.txt</h3>
      <p><b>OpenRouter</b> has human docs with code in every language. Our machine docs are excellent; our human docs are a quickstart page. <b>Spec:</b> /docs — Python + TypeScript + curl quickstarts per product, data policy, rate limits, uptime.</p>
    </div>
    <div class="card">
      <div class="stacknum"><b>P2</b> · build in public</div>
      <h3>/blog with a pulse</h3>
      <p><b>OpenRouter's</b> blog has posts dated <i>this week</i> — it signals a living company. <b>Spec:</b> weekly ship notes, benchmark posts ("we measured flat-rate pricing at 7.4× wrong — here's the data"), agent-economy analysis from our own index. The changelog is the log; the blog is the story.</p>
    </div>
    <div class="card">
      <div class="stacknum"><b>P2</b> · community</div>
      <h3>Where the builders gather</h3>
      <p><b>TypeSafe</b> launched with waitlist + Discord + hiring in one breath. <b>Spec:</b> an agent-developer Discord, featured-agent submission flow feeding the homepage wall, monthly "what agents built" roundup.</p>
    </div>
    <div class="card">
      <div class="stacknum"><b>P2</b> · status in the open</div>
      <h3>/status in the nav</h3>
      <p><b>OpenRouter</b> links uptime next to pricing. We <i>have</i> /status — it's just invisible. <b>Spec:</b> footer + developers page link it; add per-product uptime. Thirty minutes of work.</p>
    </div>
  </div>

  <div class="kicker"><span class="rh">// human-readable</span> · per-product gaps</div>

  <div class="card hl">
    <div class="stacknum"><b>01</b> — AGENTLEDGER <span class="ver">vs OpenRouter · Skyfire budgets</span></div>
    <h3>What it's missing</h3>
    <ul class="feat">
      <li><b>TypeScript SDK</b> — the wrapper is Python-only; OpenRouter is "fully OpenAI compatible" in every language. Ship <span style="font-family:var(--mono);font-size:13px">aiagentscity-ledger</span> for TS.</li>
      <li><b>Budget templates</b> — one-click packs ("Researcher: $50/mo", "Support triage: $200/mo") instead of a blank number field.</li>
      <li><b>Native Slack/Discord alerts</b> — webhooks exist; one-click integrations don't. Humans live in Slack.</li>
      <li><b>Bypass detection</b> — warn when an agent's traffic stops hitting the proxy ("your cap is blind right now"). Nobody else does this; it's the honest feature only we can claim.</li>
    </ul>
  </div>

  <div class="grid2">
    <div class="card">
      <div class="stacknum"><b>02</b> — AGENT WATCH <span class="ver">vs Skyfire KYA</span></div>
      <h3>What it's missing</h3>
      <ul class="feat">
        <li><b>The connect page it promises</b> — "launching soon" has expired. Ship it.</li>
        <li><b>Agent identity cards</b> — Skyfire's KYA gives every agent a verifiable identity; our "new agent" alerts should name <i>who</i>, with a portable identity card per caller.</li>
        <li><b>Weekly email digest</b> — the MCP brief is great; an email version reaches the humans who pay.</li>
      </ul>
    </div>
    <div class="card">
      <div class="stacknum"><b>03</b> — PERIMETER WATCH</div>
      <h3>What it's missing</h3>
      <ul class="feat">
        <li><b>Client-ready PDF reports</b> — agencies don't buy scanners, they buy <i>deliverables</i>. One-click "send the client a report" is the feature that closes deals.</li>
        <li><b>Scheduled scans + history</b> — trending over time ("3 issues fixed, 1 new this month") beats a point-in-time scan.</li>
        <li><b>Hosted dashboard</b> — MCP-only today; a URL the agency owner can open without an agent.</li>
      </ul>
    </div>
    <div class="card">
      <div class="stacknum"><b>04</b> — TRUSTSCAN</div>
      <h3>What it's missing</h3>
      <ul class="feat">
        <li><b>A public scan page</b> — paste a domain or agent ID, get a verdict with evidence. Trust you can't demo doesn't exist.</li>
        <li><b>Embeddable trust badge</b> — "Scanned by TrustScan" for marketplaces and agent directories.</li>
        <li><b>Pre-transaction API</b> — one call, <span style="font-family:var(--mono);font-size:13px">verdict + evidence</span>, built to gate x402 payments.</li>
      </ul>
    </div>
    <div class="card">
      <div class="stacknum"><b>05</b> — CITED <span class="ver">vs OpenRouter rankings</span></div>
      <h3>What it's missing</h3>
      <ul class="feat">
        <li><b>Shareable report links</b> — AgentLedger has signed, expiring links; Cited's best asset (verbatim evidence) can't be texted to anyone.</li>
        <li><b>Visibility over time</b> — OpenRouter-style trend charts: "your AI visibility is +18% this month." A single scan is a novelty; a trend is a subscription.</li>
        <li><b>The fix list</b> — don't just show what models say; show the three actions that change it. Diagnosis without prescription doesn't retain.</li>
      </ul>
    </div>
  </div>

  <div class="pull">None of this is exotic. It's the table stakes the best agent-native startups already ship — applied to a stack that's already live. P0 this week, P1 this month, P2 next.</div>

  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>

<div class="agent-surface">
  <div class="kicker"><span class="ra">// machine-readable</span> · what an agent sees on /spec</div>
  <div class="term"><div class="thead">$ agent-view /spec</div><pre>
<span class="k">gaps:</span>
  P0: [metrics-with-receipts-hero, agent-wall, homepage-onboarding-strip]
  P1: [agent-economy-index, human-billing, key-management, skeptic-faq, docs-hub]
  P2: [blog, discord, status-in-nav]
<span class="k">per_product:</span>
  agent-ledger: [ts-sdk, budget-templates, slack-alerts, bypass-detection]
  agent-watch: [connect-page, identity-cards, email-digest]
  perimeter-watch: [pdf-reports, scan-history, hosted-dashboard]
  trustscan: [public-scan-page, trust-badge, pre-tx-api]
  cited: [shareable-links, visibility-trends, fix-list]
<span class="k">method:</span> benchmarked vs openrouter.ai, typesafe.ai, skyfire.xyz (2026-09-18)
</pre></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>"""


PAGES_META = {
    "/": ("PAGE_HOME", "AI Agent City", "AI Agent City is the operations layer for the agent economy: spending limits, monitoring, security, trust, and discovery for AI agents. Human-readable and machine-readable."),
    "/products": ("PAGE_PRODUCTS", "Products — AI Agent City", "Five products, one thesis: the agent economy needs operations. AgentLedger, Agent Watch, Perimeter Watch, TrustScan, and Cited — each live, independently usable, and agent-callable."),
    "/developers": ("PAGE_DEVELOPERS", "Developers — AI Agent City", "MCP, REST, CLI, and the x402 purchase path. Machine-readable surfaces for all five products."),
    "/compare": ("PAGE_COMPARE", "AI Agent City vs trace viewers — AI Agent City", "How per-agent budget enforcement differs from request-level trace observability, with honestly dated list prices."),
    "/changelog": ("PAGE_CHANGELOG", "Changelog — AI Agent City", "External-facing product releases across the AI Agent City suite. Human-readable and machine-readable."),
    "/spec": ("PAGE_SPEC", "Founder gap spec — AI Agent City", "What the site and each product are still missing, written like a founder. Prioritized P0/P1/P2."),
}
