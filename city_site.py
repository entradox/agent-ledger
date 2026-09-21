"""The AI Agent City umbrella site: one site, two readers.

Generated from the two-surfaces mockup by hidden_files/build_city_site.py —

re-run that script after mockup edits instead of hand-editing this file.

Quiet light system (Cited-style): paper background, indigo agent accent,
Agent surface first, human surface second,

toggled per page via the Human | Agent segmented control.

"""
from __future__ import annotations

CSS = """  :root{
    --paper:#FAFAF8; --card:#FFFFFF; --ink:#1B1B18; --muted:#6E6E68; --faint:#A3A39B;
    --line:#E9E7E1; --line-soft:#F1EFE9;
    --accent:#4F46E5; --accent-ink:#4338CA; --accent-deep:#3730A3;
    --accent-soft:#EFF0FE; --accent-line:#DCDDFB;
    --human:#A16207; --agent:#4F46E5; --agent-dim:#DCDDFB;
    --txt:#1B1B18; --mut:#6E6E68; --dim:#A3A39B; --panel:#FFFFFF; --panel2:#F1EFE9; --bg:#FAFAF8;
    --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
    --sans:-apple-system,BlinkMacSystemFont,"Inter","Segoe UI",Roboto,Helvetica,Arial,sans-serif;
    --sh:0 1px 2px rgba(27,27,24,.05),0 4px 16px rgba(27,27,24,.05);
    --ease:cubic-bezier(.22,.68,0,1.02);
  }
  *{box-sizing:border-box;margin:0;padding:0}
  body{background:var(--paper);color:var(--ink);font-family:var(--sans);
    font-size:15px;line-height:1.65;-webkit-font-smoothing:antialiased;text-rendering:optimizeLegibility}
  .wrap{max-width:1060px;margin:0 auto;padding:0 20px}
  /* top bar: wordmark + tabs + surface toggle */
  .topbar{border-bottom:1px solid var(--line);background:rgba(250,250,248,.9);
    backdrop-filter:blur(12px);-webkit-backdrop-filter:blur(12px);
    position:sticky;top:0;z-index:60}
  .nav-in{max-width:1060px;margin:0 auto;padding:11px 20px;display:flex;
    align-items:center;gap:14px}
  .wordmark{display:flex;align-items:center;gap:8px;font-family:var(--mono);
    font-size:12px;letter-spacing:.14em;text-transform:uppercase;font-weight:600;
    color:var(--ink);text-decoration:none;white-space:nowrap}
  .wordmark .mark{width:14px;height:14px;border-radius:4px;flex-shrink:0;
    background:linear-gradient(135deg,var(--accent),var(--accent-deep));
    box-shadow:0 2px 6px rgba(79,70,229,.35)}
  .tabs{display:flex;gap:2px;overflow-x:auto;flex:1;scrollbar-width:none}
  .tabs::-webkit-scrollbar{display:none}
  .tabs a{font-family:var(--mono);font-size:12px;color:var(--muted);text-decoration:none;
    padding:8px 12px;border-radius:9px;white-space:nowrap}
  .tabs a:hover{color:var(--ink);background:var(--line-soft)}
  .tabs a.active{color:var(--accent-ink);background:var(--accent-soft);font-weight:600}
  body.agent-view .tabs a.active{color:var(--accent-ink)}
  .toggle{position:relative;display:flex;background:#EDECE7;border-radius:99px;
    padding:3px;isolation:isolate;flex-shrink:0}
  .toggle .ind{position:absolute;top:3px;bottom:3px;left:0;border-radius:99px;
    background:var(--accent);transition:transform .38s var(--ease),width .38s var(--ease);z-index:0}
  .toggle button{position:relative;z-index:1;border:0;background:transparent;
    font-family:var(--mono);font-size:11px;letter-spacing:.1em;padding:7px 15px;
    border-radius:99px;color:var(--muted);cursor:pointer;text-transform:uppercase;transition:color .3s}
  .toggle button.on{color:#fff}
  .toggle button:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
  @media(max-width:640px){.nav-in{flex-wrap:wrap}.tabs{order:3;flex-basis:100%}}
  /* surfaces */
  .page{display:none;max-width:1060px;margin:0 auto;padding:48px 20px 72px}
  .page.active{display:block}
  .agent-surface{display:none}
  body.agent-view .human-surface{display:none!important}
  body.agent-view .agent-surface{display:block!important;animation:surfIn .5s var(--ease)}
  @keyframes surfIn{from{opacity:0;transform:translateY(10px)}to{opacity:1;transform:none}}
  /* type */
  h1{font-size:31px;line-height:1.16;letter-spacing:-.025em;font-weight:700;margin-bottom:14px;max-width:17em}
  h2{font-size:21px;letter-spacing:-.015em;font-weight:700;margin:0 0 10px}
  h3{font-size:17px;margin:0 0 7px;letter-spacing:-.012em;font-weight:700}
  @media(min-width:900px){h1{font-size:44px}}
  .kicker{font-family:var(--mono);font-size:10.5px;letter-spacing:.2em;color:var(--faint);
    text-transform:uppercase;margin-bottom:16px}
  .kicker .rh{color:var(--muted)} .kicker .ra{color:var(--accent-ink)}
  section{padding:48px 0;border-bottom:1px solid var(--line-soft)}
  .lede{font-size:15px;color:var(--muted);max-width:36em;margin-bottom:24px}
  .lede b{color:var(--ink);font-weight:600}
  .cta-row{display:flex;gap:10px;flex-wrap:wrap;margin:22px 0;align-items:center}
  .btn{display:inline-block;padding:13px 24px;border-radius:13px;font-weight:600;font-size:14px;
    text-decoration:none;cursor:pointer;border:1px solid transparent;transition:transform .15s,box-shadow .2s,background .2s}
  .btn:active{transform:scale(.97)}
  .btn:focus-visible{outline:2px solid var(--accent);outline-offset:3px}
  .btn-human{background:var(--accent);color:#fff;box-shadow:0 4px 14px rgba(79,70,229,.28)}
  .btn-human:hover{background:var(--accent-deep)}
  .btn-agentb{background:transparent;color:var(--accent-ink);border-color:var(--accent-line);
    font-family:var(--mono);font-size:13px;font-weight:400}
  .btn-agentb:hover{border-color:var(--accent)}
  .btn-ghost{background:var(--card);color:var(--ink);border-color:var(--line)}
  .btn-ghost:hover{border-color:var(--ink)}
  .hero-grid{display:grid;grid-template-columns:1fr;gap:28px;margin-top:8px}
  @media(min-width:900px){.hero-grid{grid-template-columns:1.05fr .95fr;gap:40px;align-items:center}}
  /* agent hero wash */
  .agent-hero{position:relative;overflow:hidden;background:var(--accent-soft);
    border:1px solid var(--accent-line);border-radius:18px;padding:26px 22px;margin-bottom:10px}
  .agent-hero::before{content:"";position:absolute;inset:0;pointer-events:none;
    background:radial-gradient(420px 200px at 85% -20%,rgba(79,70,229,.14),transparent 70%)}
  .agent-hero>*{position:relative}
  .agent-hero .lede{margin-bottom:0}
  /* live terminal */
  .term{background:#101013;color:#D8D8D2;border-radius:16px;overflow:hidden;
    margin:8px 0 12px;font-family:var(--mono);font-size:12px;
    box-shadow:0 12px 32px rgba(16,16,19,.22),0 2px 6px rgba(16,16,19,.25)}
  .term .bar{display:flex;align-items:center;gap:7px;padding:11px 15px;
    border-bottom:1px solid #232327;background:#17171a}
  .term .bar i{width:10px;height:10px;border-radius:50%;display:block}
  .term .bar i:nth-child(1){background:#FF5F57;opacity:.85}
  .term .bar i:nth-child(2){background:#FEBC2E;opacity:.85}
  .term .bar i:nth-child(3){background:#28C840;opacity:.85}
  .term .bar span{margin-left:7px;color:#8E8E96;font-size:10px;letter-spacing:.16em;text-transform:uppercase}
  .term .bar .live{margin-left:auto;color:#6EE7A8;display:flex;align-items:center;gap:6px;
    font-size:10px;letter-spacing:.16em}
  .term .bar .live::before{content:"";width:7px;height:7px;border-radius:50%;
    background:#34D399;animation:pulse 2s infinite}
  @keyframes pulse{0%,100%{box-shadow:0 0 0 0 rgba(52,211,153,.5)}50%{box-shadow:0 0 0 5px rgba(52,211,153,0)}}
  .term .rows{padding:8px 16px 12px}
  .term .step{padding:10px 0;border-bottom:1px solid #1E1E22;opacity:0;
    animation:stepIn .55s var(--ease) forwards}
  .term .step:nth-child(1){animation-delay:.15s}.term .step:nth-child(2){animation-delay:.55s}
  .term .step:nth-child(3){animation-delay:.95s}.term .step:nth-child(4){animation-delay:1.35s}
  @keyframes stepIn{from{opacity:0;transform:translateY(6px)}to{opacity:1;transform:none}}
  .term .step:last-child{border:0}
  .term .step .t{color:#71717A;font-size:10px;letter-spacing:.16em;text-transform:uppercase;display:block;margin-bottom:4px}
  .term .step .c{color:#F4F4F2;word-break:break-all;display:block}
  .term .step .r{color:#6EE7A8;display:block}
  .term .step .r b{color:#A7F3D0;font-weight:600}
  .cursor{display:inline-block;width:7px;height:13px;background:#A5B4FC;
    vertical-align:-2px;margin-left:3px;animation:blink 1.1s steps(1) infinite}
  @keyframes blink{50%{opacity:0}}
  .note{font-family:var(--mono);font-size:11px;color:var(--faint);margin-bottom:28px}
  .note::before{content:"▲ "}
  /* legacy agent-surface terminal (other pages) */
  .agent-surface .term .thead{padding:12px 18px;border-bottom:1px solid #232327;
    font-family:var(--mono);font-size:12px;color:#A5B4FC;background:#17171a}
  .agent-surface .term pre{border:none;margin:0;border-radius:0;background:transparent;box-shadow:none}
  .agent-note{font-family:var(--mono);font-size:12px;color:var(--faint);margin:14px 0;max-width:46em;line-height:1.7}
  .agent-note b{color:var(--accent-ink);font-weight:600}
  /* connect list */
  .conn{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:4px 0;margin-bottom:8px;box-shadow:var(--sh)}
  .conn-row{display:flex;align-items:center;gap:10px;padding:12px 16px;
    border-bottom:1px solid var(--line-soft);font-family:var(--mono);font-size:12px}
  .conn-row:last-child{border:0}
  .conn-row .nm{color:var(--accent-ink);font-weight:600;white-space:nowrap}
  .conn-row .cnt{font-size:10px;color:var(--faint);background:var(--paper);
    border:1px solid var(--line);border-radius:99px;padding:2px 8px;white-space:nowrap}
  .conn-row .url{color:var(--muted);flex:1;min-width:0;overflow:hidden;
    text-overflow:ellipsis;white-space:nowrap}
  .copy{border:1px solid var(--line);background:var(--paper);border-radius:9px;
    font-family:var(--mono);font-size:10px;letter-spacing:.08em;color:var(--muted);
    padding:6px 11px;cursor:pointer;text-transform:uppercase;white-space:nowrap;transition:all .18s}
  .copy:hover{border-color:var(--accent);color:var(--accent-ink)}
  .copy:active{transform:scale(.94)}
  .copy.ok{background:var(--accent);border-color:var(--accent);color:#fff}
  .buy{background:var(--accent-soft);border:1px solid var(--accent-line);
    border-radius:16px;padding:18px;margin:20px 0 0}
  .buy .lbl{font-family:var(--mono);font-size:10px;letter-spacing:.18em;
    text-transform:uppercase;color:var(--accent-ink);display:block;margin-bottom:8px;opacity:.75}
  .buy p{font-family:var(--mono);font-size:12.5px;line-height:1.7;color:var(--accent-deep);margin:0}
  .buy b{font-weight:700}
  /* rails pills */
  .rails{display:flex;gap:8px;flex-wrap:wrap;margin:28px 0;padding:20px 0;
    border-top:1px solid var(--line-soft);border-bottom:1px solid var(--line-soft)}
  .rails span{font-family:var(--mono);font-size:11px;letter-spacing:.05em;color:var(--faint);
    border:1px solid var(--line);border-radius:99px;padding:8px 15px;background:var(--card)}
  .rails span b{color:var(--ink);font-weight:600}
  .rails .rl{font-size:10px;color:var(--faint);border:none;background:none;
    padding:8px 4px;letter-spacing:.18em;text-transform:uppercase}
  /* thesis */
  .thesis{display:grid;grid-template-columns:1fr;gap:12px;margin:26px 0}
  @media(min-width:900px){.thesis{grid-template-columns:1fr 1fr 1fr}}
  .thesis .t{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:22px 20px;box-shadow:var(--sh)}
  .thesis .t .n{font-family:var(--mono);color:var(--accent-ink);font-size:12px;margin-bottom:8px}
  .thesis .t h4{font-size:15px;margin-bottom:6px}
  .thesis .t p{font-size:13.5px;color:var(--muted);margin:0}
  .pull{border-left:3px solid var(--accent);padding:6px 0 6px 20px;margin:28px 0;
    font-size:19px;color:var(--ink);font-weight:600;max-width:36em;letter-spacing:-.01em}
  /* product cards */
  .stacknum{display:flex;justify-content:space-between;align-items:baseline;gap:8px;
    font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;text-transform:uppercase;
    color:var(--faint);margin-bottom:10px}
  .stacknum b{color:var(--ink);font-weight:600}
  .stacknum .tc{color:var(--accent-ink);background:var(--accent-soft);
    border-radius:99px;padding:2px 9px;letter-spacing:.06em;white-space:nowrap}
  .card{background:var(--card);border:1px solid var(--line);border-radius:16px;
    padding:22px 20px;margin-bottom:12px;box-shadow:var(--sh);
    transition:transform .25s var(--ease),box-shadow .25s}
  .card:hover{transform:translateY(-2px);
    box-shadow:0 2px 4px rgba(27,27,24,.06),0 10px 28px rgba(27,27,24,.08)}
  .card.hl{border:1.5px solid var(--ink)}
  .card p{color:var(--muted);font-size:14px;margin-bottom:14px;max-width:40em}
  .card p b{color:var(--ink)}
  .card .links{display:flex;gap:18px;flex-wrap:wrap;font-size:13.5px;margin-top:6px}
  .card .links a{color:var(--ink);text-decoration:none;font-weight:600}
  .card .links a:hover{text-decoration:underline}
  .card .links a.ag{color:var(--accent-ink);font-family:var(--mono);font-size:12px;font-weight:400}
  .grid2{display:grid;grid-template-columns:1fr;gap:12px}
  .grid3{display:grid;grid-template-columns:1fr;gap:12px}
  @media(min-width:900px){.grid2{grid-template-columns:1fr 1fr}.grid3{grid-template-columns:1fr 1fr 1fr}}
  .pills{display:flex;gap:8px;flex-wrap:wrap;margin:16px 0 4px}
  .pill{font-family:var(--mono);font-size:11px;letter-spacing:.05em;
    border:1px solid var(--line);border-radius:99px;padding:8px 15px;
    color:var(--faint);background:var(--card)}
  .pill.on{border-color:var(--accent-line);color:var(--accent-ink);background:var(--accent-soft)}
  pre{background:#101013;border:1px solid #232327;border-radius:12px;padding:18px;overflow-x:auto;
    font-family:var(--mono);font-size:12.5px;line-height:1.65;color:#D8D8D2;margin:14px 0;position:relative}
  pre .c{color:#71717A} pre .k{color:#A5B4FC} pre .s{color:#6EE7A8}
  .copybtn{position:absolute;top:10px;right:10px;background:#1c1c21;border:1px solid #2c2c33;color:#a1a1aa;
    font-family:var(--mono);font-size:11px;padding:4px 10px;border-radius:8px;cursor:pointer}
  ul.feat{list-style:none;margin:14px 0}
  ul.feat li{padding:10px 0 10px 28px;border-bottom:1px solid var(--line-soft);font-size:14.5px;
    color:var(--muted);position:relative}
  ul.feat li:before{content:"→";position:absolute;left:4px;color:var(--accent-ink)}
  ul.feat li b{color:var(--ink)}
  ul.feat li:last-child{border-bottom:none}
  table.cmp{width:100%;border-collapse:collapse;font-size:14px;margin-top:16px;
    background:var(--card);border-radius:12px;overflow:hidden;box-shadow:var(--sh)}
  table.cmp th,table.cmp td{border:1px solid var(--line);padding:12px 14px;text-align:left;vertical-align:top}
  table.cmp th{background:var(--paper);font-family:var(--mono);font-size:12px}
  table.cmp td.y{color:#15803d} table.cmp td.n{color:var(--faint)} table.cmp td.part{color:var(--human)}
  table.cmp .note{font-size:12px;color:var(--faint);display:block;margin-top:6px}
  .us{color:var(--accent-ink);font-weight:700}
  .chlog{border-left:2px solid var(--line);margin:20px 0 0 8px;padding-left:24px}
  .chlog .e{margin-bottom:22px;position:relative}
  .chlog .e:before{content:"";position:absolute;left:-31px;top:6px;width:10px;height:10px;
    border-radius:50%;background:var(--accent)}
  .chlog .d{font-family:var(--mono);font-size:12px;color:var(--faint)}
  .chlog .t{font-size:14.5px;margin-top:2px;color:var(--muted)}
  .chlog .t b{color:var(--ink)}
  .ver{font-family:var(--mono);font-size:11px;color:var(--faint);margin-left:8px}
  .illus{font-family:var(--mono);font-size:11px;color:var(--faint);margin-top:10px}
  footer.site{border-top:1px solid var(--line);margin-top:64px;padding:28px 0;color:var(--faint);
    font-size:13px;display:flex;gap:18px;flex-wrap:wrap;justify-content:space-between}
  footer.site a{color:var(--muted);text-decoration:none;margin-right:16px;font-family:var(--mono);font-size:12px}
  footer.site a:hover{color:var(--ink)}
  .fine{font-family:var(--mono);font-size:11px;color:var(--faint);margin-top:24px;line-height:2}
  .fine b{color:var(--muted);font-weight:400}
  @media(prefers-reduced-motion:reduce){
    *,*::before,*::after{animation-duration:.01ms!important;transition-duration:.01ms!important}
    .term .step{opacity:1}
  }
"""

TOGGLE_JS = """<script>
function setView(v){
  var a = v==='agent';
  document.body.classList.toggle('agent-view', a);
  var h=document.getElementById('segH'), g=document.getElementById('segA');
  h.classList.toggle('on', !a); g.classList.toggle('on', a);
  h.setAttribute('aria-selected', !a); g.setAttribute('aria-selected', a);
  moveInd(a?'segA':'segH');
  document.querySelectorAll('.agent-surface .term .step').forEach(function(s){
    s.style.animation='none'; void s.offsetWidth; s.style.animation='';
  });
  window.scrollTo({top:0});
}
function moveInd(id){
  var b=document.getElementById(id), ind=document.getElementById('segInd');
  if(!b||!ind) return;
  ind.style.width=b.offsetWidth+'px';
  ind.style.transform='translateX('+(b.offsetLeft-2)+'px)';
}
window.addEventListener('load',function(){
  moveInd(document.body.classList.contains('agent-view')?'segA':'segH');
});
window.addEventListener('resize',function(){
  moveInd(document.getElementById('segA').classList.contains('on')?'segA':'segH');
});
function copyPre(btn){
  var pre=btn.parentElement;
  var text=Array.from(pre.childNodes).filter(function(n){return n!==btn;}).map(function(n){return n.textContent;}).join('');
  navigator.clipboard.writeText(text.trim()).then(function(){btn.textContent='copied';setTimeout(function(){btn.textContent='copy'},1200);});
}
function copyCmd(i,btn){
  var done=function(){btn.textContent='copied';btn.classList.add('ok');setTimeout(function(){btn.textContent='copy';btn.classList.remove('ok')},1400)};
  if(navigator.clipboard&&navigator.clipboard.writeText){navigator.clipboard.writeText(HOME_CMDS[i]).then(done).catch(done)}else{done()}
}
</script>"""

SHELL = """<!doctype html><html lang="en"><head><meta charset="utf-8">
<title>__TITLE__</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="description" content="__DESC__">
<style>
""" + CSS + """
</style></head>
<body class="agent-view">
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
]


def nav(active: str) -> str:
    tabs = []
    for path, label, style in TABS:
        cls = ' class="active"' if path == active else ""
        tabs.append(f'<a href="{path}"{cls}>{label}</a>')
    return (
        '<nav class="topbar"><div class="nav-in">'
        '<a class="wordmark" href="/"><span class="mark"></span>AI Agent City</a>'
        '<div class="tabs">' + "".join(tabs) + "</div>"
        '<div class="toggle" role="tablist" aria-label="Surface"><span class="ind" id="segInd"></span>'
        '<button id="segA" class="on" role="tab" aria-selected="true" '
        'onclick="setView(\'agent\')">Agent</button>'
        '<button id="segH" role="tab" aria-selected="false" '
        'onclick="setView(\'human\')">Human</button>'
        "</div></div></nav>"
    )




def render(active: str, title: str, desc: str, body: str) -> str:
    """Render a city page, with the x402 network DERIVED from config.

    Hardcoding "Base mainnet" in these literals is what let the site contradict
    production: the network is set by X402_NETWORK, so a literal goes stale the
    moment that value changes — and it did. Production runs __NETWORK__ while the
    code default is __NETWORK__2. Substituting here, once, for every city page and
    on every request, means no page under this module can disagree with the
    deployment again.

    Tokens (deliberately __-delimited, not {}: these literals contain CSS braces):
      __NETWORK__       __NETWORK__ or __NETWORK__2
      __NETWORK_LABEL__ ""Base mainnet" or "Base Sepolia testnet"
      __NETWORK_PROSE__ full settlement sentence for the live mode
    """
    import x402_verify
    label = "Base mainnet" if x402_verify.X402_NETWORK in getattr(
        x402_verify, "_MAINNET_NETWORKS", set()) else "Base Sepolia testnet"
    try:
        import api_server as _api
        prose = _api._x402_settlement_span()
    except Exception:
        prose = ""
    body = (body
            .replace("__NETWORK_LABEL__", label)
            .replace("__NETWORK__", x402_verify.X402_NETWORK)
            .replace("__NETWORK_PROSE__", prose))

    html = SHELL
    html = html.replace("__TITLE__", title)
    html = html.replace("__DESC__", desc)
    html = html.replace("__NAV__", nav(active))
    html = html.replace("__BODY__", body)
    return html


PAGE_HOME = """<div class="agent-surface">
<section>
  <div class="agent-hero">
    <div class="kicker"><span class="ra">// machine-readable</span> · ai agent city</div>
    <h1>The operations layer agents run on.</h1>
    <p class="lede">Budgets, monitoring, security, trust, discovery — <b>five products, one MCP catalog, zero human required.</b> Read the contract, connect, transact.</p>
  </div>
  <div class="kicker"><span class="ra">// live session</span></div>
  <div class="term">
    <div class="bar"><i></i><i></i><i></i><span>agent session</span><span class="live">live</span></div>
    <div class="rows">
      <div class="step"><span class="t">discover</span><span class="c">GET /.well-known/x402.json</span><span class="r"><b>200</b> · mainnet __NETWORK__ · accepts USDC</span></div>
      <div class="step"><span class="t">connect</span><span class="c">mcp add agent-ledger https://aiagentscity.com/mcp/</span><span class="r"><b>12 tools</b> · ledger_track · ledger_set_budget …</span></div>
      <div class="step"><span class="t">transact</span><span class="c">POST /v1/billing/x402 · X-PAYMENT: &lt;signed&gt;</span><span class="r"><b>200</b> · 24h Pro · $0.01 USDC settled</span></div>
      <div class="step"><span class="t">enforce</span><span class="c">ledger_set_budget {agent:"researcher", monthly:$50}</span><span class="r">cap armed · over-budget calls → <b>402</b> pre-provider<span class="cursor"></span></span></div>
    </div>
  </div>
  <p class="note">the agent-native surface, playing next to the contract — the whole site works like this</p>
  <div class="kicker"><span class="ra">// connect</span> · one command per product</div>
  <div class="conn"><div class="conn-row"><span class="nm">agent-ledger</span><span class="cnt">12 tools</span><span class="url">claude mcp add --transport http agent-ledger https://aiagentscity.com/mcp/</span><button class="copy" onclick="copyCmd(0,this)" aria-label="Copy connect command for agent-ledger">copy</button></div><div class="conn-row"><span class="nm">agent-watch</span><span class="cnt">8 tools</span><span class="url">claude mcp add --transport http agent-watch https://aiagentscity.com/mcp/agent-watch/</span><button class="copy" onclick="copyCmd(1,this)" aria-label="Copy connect command for agent-watch">copy</button></div><div class="conn-row"><span class="nm">perimeter-watch</span><span class="cnt">6 tools</span><span class="url">claude mcp add --transport http perimeter-watch https://aiagentscity.com/mcp/perimeter-watch/</span><button class="copy" onclick="copyCmd(2,this)" aria-label="Copy connect command for perimeter-watch">copy</button></div><div class="conn-row"><span class="nm">trustscan</span><span class="cnt">4 tools</span><span class="url">claude mcp add --transport http trustscan https://aiagentscity.com/mcp/trustscan/</span><button class="copy" onclick="copyCmd(3,this)" aria-label="Copy connect command for trustscan">copy</button></div><div class="conn-row"><span class="nm">cited</span><span class="cnt">9 tools</span><span class="url">claude mcp add --transport http cited https://aiagentscity.com/mcp/cited/</span><button class="copy" onclick="copyCmd(4,this)" aria-label="Copy connect command for cited">copy</button></div></div>
  <div class="buy">
    <span class="lbl">x402 · __NETWORK_LABEL__</span>
    <p><b>$0.01 · zero clicks.</b><br>POST /v1/billing/x402 + X-PAYMENT header → 24h AgentLedger Pro on the workspace your wallet resolves to. No signup flow, no card form, no human.</p>
  </div>
</section>
<section>
  <div class="kicker"><span class="ra">// machine-readable</span> · the stack</div>
  <h2>Five products, one contract.</h2>
  <p class="lede">Every product is MCP-native. Tool counts are the live <span style="font-family:var(--mono);font-size:13px">tools/list</span> output, not marketing.</p>
  <div class="card hl"><div class="stacknum"><b>01 · Control spend · Flagship</b><span class="tc">12 tools</span></div><h3>AgentLedger — spending limits for AI agents</h3><p>Per-agent spend caps with 402 enforcement before the provider is ever called. The call that would break the budget <b>never reaches the provider</b>.</p><div class="links"><a class="ag" href="/developers">$ mcp add agent-ledger →</a></div></div><div class="card"><div class="stacknum"><b>02 · Monitor</b><span class="tc">8 tools</span></div><h3>Agent Watch</h3><p>Monitoring for the agent economy. Know the moment a new agent touches your API.</p><div class="links"><a class="ag" href="/developers">$ mcp add agent-watch →</a></div></div><div class="card"><div class="stacknum"><b>03 · Secure</b><span class="tc">6 tools</span></div><h3>Perimeter Watch</h3><p>Dangling DNS, expiring certs, lookalike domains — caught before they're incidents.</p><div class="links"><a class="ag" href="/developers">$ mcp add perimeter-watch →</a></div></div><div class="card"><div class="stacknum"><b>04 · Trust</b><span class="tc">4 tools</span></div><h3>TrustScan</h3><p>Scan before you trust. Know who you're dealing with before your agent does.</p><div class="links"><a class="ag" href="/developers">$ mcp add trustscan →</a></div></div><div class="card"><div class="stacknum"><b>05 · Be found</b><span class="tc">9 tools</span></div><h3>Cited</h3><p>Does AI recommend your practice? Instant scan, verbatim evidence.</p><div class="links"><a class="ag" href="/developers">$ mcp add cited →</a></div></div>
  <div class="fine"><b>machine entry points</b><br>/.well-known/x402.json · /skill.md · /openapi.json · /server.json · /.well-known/mcp.json · /status · /llms.txt</div>
  <footer class="site"><div><a href="/products">/products</a><a href="/developers">/developers</a><a href="/llms.txt">/llms.txt</a><a href="/status">status</a></div><div>AI Agent City · one site, two readers</div></footer>
</section>
</div>

<div class="human-surface">
<section>
  <div class="kicker"><span class="rh">// human-readable</span> · ai agent city</div>
  <h1>Agents are economic actors now.</h1>
  <p class="lede">They spend money. They call your APIs. They represent businesses. <b>AI Agent City is the operations layer they run on</b> — budgets, monitoring, security, trust, and discovery. Live software, not slides.</p>
  <div class="cta-row">
    <a class="btn btn-human" href="/products">Explore the stack</a>
    <a class="btn btn-ghost" href="/start">Start free</a>
    <a class="btn btn-ghost" href="/demo">Try the live demo →</a>
  </div>
  <div class="pills">
    <span class="pill on">MCP · tool protocol</span>
    <span class="pill on">x402 · payment protocol</span>
    <span class="pill">Base · __NETWORK__</span>
    <span class="pill">USDC · settlement</span>
    <span class="pill">llms.txt · discovery</span>
  </div>
</section>
<section>
  <div class="kicker"><span class="rh">// human-readable</span> · why now</div>
  <div class="thesis">
    <div class="t"><div class="n">01</div><h4>Agents hold wallets</h4><p>Machine-to-machine payments settle on-chain for fractions of a cent. The moment agents spend, someone has to set the budget.</p></div>
    <div class="t"><div class="n">02</div><h4>Agents call your APIs</h4><p>Non-human traffic already hits production endpoints, and most teams can't see it. You can't secure or bill what you can't attribute.</p></div>
    <div class="t"><div class="n">03</div><h4>AI answers are the new search</h4><p>Customers ask ChatGPT and Claude instead of Googling. If the models don't recommend you — with receipts — you're invisible.</p></div>
  </div>
  <div class="pull">"Every economic actor needs five things: a budget, a monitor, a perimeter, a reputation, and a way to be found. We're building all five — for agents."</div>
</section>
<section>
  <div class="kicker"><span class="rh">// human-readable</span> · the stack</div>
  <h2>One stack. Five products.</h2>
  <p class="lede">Each solves one operational problem end to end. Each is <b>live and independently usable.</b></p>
  <div class="card hl"><div class="stacknum"><b>01 · Control spend · Flagship</b><span class="tc">12 tools</span></div><h3>AgentLedger — spending limits for AI agents</h3><p>Per-agent spend caps with 402 enforcement before the provider is ever called. The call that would break the budget <b>never reaches the provider</b>.</p><div class="links"><a href="/products">Explore →</a><a class="ag" href="/developers">$ mcp add agent-ledger →</a></div></div><div class="card"><div class="stacknum"><b>02 · Monitor</b><span class="tc">8 tools</span></div><h3>Agent Watch</h3><p>Monitoring for the agent economy. Know the moment a new agent touches your API.</p><div class="links"><a href="/products">Explore →</a><a class="ag" href="/developers">$ mcp add agent-watch →</a></div></div><div class="card"><div class="stacknum"><b>03 · Secure</b><span class="tc">6 tools</span></div><h3>Perimeter Watch</h3><p>Dangling DNS, expiring certs, lookalike domains — caught before they're incidents.</p><div class="links"><a href="/products">Explore →</a><a class="ag" href="/developers">$ mcp add perimeter-watch →</a></div></div><div class="card"><div class="stacknum"><b>04 · Trust</b><span class="tc">4 tools</span></div><h3>TrustScan</h3><p>Scan before you trust. Know who you're dealing with before your agent does.</p><div class="links"><a href="/products">Explore →</a><a class="ag" href="/developers">$ mcp add trustscan →</a></div></div><div class="card"><div class="stacknum"><b>05 · Be found</b><span class="tc">9 tools</span></div><h3>Cited</h3><p>Does AI recommend your practice? Instant scan, verbatim evidence.</p><div class="links"><a href="/products">Explore →</a><a class="ag" href="/developers">$ mcp add cited →</a></div></div>
</section>
<section>
  <div class="kicker"><span class="rh">// human-readable</span> · proof</div>
  <h2>Live, not slides.</h2>
  <div class="grid3" style="margin-top:16px">
    <div class="card"><h3>$0.01, zero clicks</h3><p>An agent bought 24h of AgentLedger Pro over x402 on __NETWORK_LABEL__ — real USDC, no human in the loop.</p></div>
    <div class="card"><h3>402, not a dashboard</h3><p>Trace viewers report after you've paid. AgentLedger refuses the over-budget call <b>before the provider sees it</b>.</p></div>
    <div class="card"><h3>Priced honestly</h3><p>Flat-rate token pricing was 7.4× wrong on a real session. AgentLedger prices cache-aware.</p></div>
  </div>
  <div class="cta-row"><a class="btn btn-ghost" href="/compare">AgentLedger vs trace viewers →</a><a class="btn btn-agentb" href="/developers">I'm an agent — take me to /developers →</a></div>
  <div class="kicker" style="margin-top:28px"><span class="rh">// human-readable</span> · what's new</div>
  <h2>Recent ships, dated.</h2>
  <p class="lede">The changelog is the proof the stack is alive — every entry is a shipped outcome, not a progress update.</p>
  <div class="chlog" style="margin-top:14px">
    <div class="e"><div class="d">2026-09-21</div><div class="t"><b>Access vs spend.</b> A positioning page: <span class="ver">/manifesto</span> — Kiteworks governs what agents can touch, AgentLedger governs what agents can spend. Complementary halves, and only one of them has an owner yet. <span class="ver">platform</span></div></div>
    <div class="e"><div class="d">2026-09-20</div><div class="t"><b>MCP verified end-to-end.</b> Handshake and <span class="ver">tools/list</span> confirmed for all five products — 12, 8, 6, 4 and 9 tools. The stale "dispatch needs repair" warnings were retired from every product page. <span class="ver">platform</span></div></div>
    <div class="e"><div class="d">2026-09-19</div><div class="t"><b>Satellite MCP endpoints mounted.</b> <span class="ver">/mcp/agent-watch</span>, <span class="ver">/mcp/perimeter-watch</span>, <span class="ver">/mcp/cited</span> and <span class="ver">/mcp/trustscan</span> resolve instead of 404ing. <span class="ver">platform</span></div></div>
    <div class="e"><div class="d">2026-09-16</div><div class="t"><b>x402 live on __NETWORK_LABEL__.</b> $0.01 USDC → 24h of AgentLedger Pro. Agents buy with zero human clicks. <span class="ver">agent-ledger</span></div></div>
  </div>
  <div class="cta-row"><a class="btn btn-ghost" href="/changelog">Full changelog →</a></div>
  <footer class="site">
    <div><a href="/products">/products</a><a href="/developers">/developers</a><a href="/compare">/compare</a><a href="/changelog">/changelog</a><a href="/llms.txt">/llms.txt</a><a href="/status">status</a><a href="/security">security</a></div>
    <div>AI Agent City · one site, two readers</div>
  </footer>
</section>
</div>
<script>var HOME_CMDS = ["claude mcp add --transport http agent-ledger https://aiagentscity.com/mcp/", "claude mcp add --transport http agent-watch https://aiagentscity.com/mcp/agent-watch/", "claude mcp add --transport http perimeter-watch https://aiagentscity.com/mcp/perimeter-watch/", "claude mcp add --transport http trustscan https://aiagentscity.com/mcp/trustscan/", "claude mcp add --transport http cited https://aiagentscity.com/mcp/cited/"];</script>"""


PAGE_MANIFESTO = """<div class="agent-surface">
<section>
<h2>Two halves of one problem. Only one has an owner.</h2>
  <p class="lede">A governed agent needs two things it can be trusted with: what it may
  <b>touch</b>, and what it may <b>spend</b>. Every agent that acts economically needs a
  budget before it needs an audit log — the spend decision happens first, on every call.</p>
  <div class="grid3" style="margin-top:16px">
    <div class="card"><h3>Access governance</h3><p>What an agent is allowed to reach:
    inherited RBAC, encryption, audit trails, credentials kept out of model context. This
    is real, it is being solved, and enterprise incumbents are solving it — Kiteworks
    launched a marketplace of 60+ governed agents running through their Secure MCP server
    on 2026-09-17.</p></div>
    <div class="card"><h3>Spend governance</h3><p>What an agent is allowed to spend, and
    what happens the moment it exceeds that. Metres it, caps it, refuses the over-budget
    call <b>before the provider sees it</b>, then keeps a ledger. This layer has no owner
    yet.</p></div>
    <div class="card"><h3>Why they are different products</h3><p>Access control answers
    "may this agent read that?" Spend control answers "may this agent buy that, and is it
    already over budget?" A perfect access-control system still does not know an agent has
    quietly spent its way past a budget.</p></div>
  </div>
  <h2>The line</h2>
  <p><b>Kiteworks governs what agents can touch. AgentLedger governs what agents can
  spend.</b></p>
  <p class="lede">Complementary, not competitive — two halves of one trust problem. If you
  are governing what your agents can reach, the other side of the same call is still
  unowned.</p>
  <h2>What we claim, and what we don't</h2>
  <ul>
    <li><b>Claimed:</b> an agent can buy 24 hours of AgentLedger Pro for $0.01 USDC over
    x402 on __NETWORK_LABEL__, no human in the loop. It settled on mainnet.</li>
    <li><b>Claimed:</b> a call that would exceed a set budget returns 402 and is refused
    before it reaches the provider — not logged afterwards.</li>
    <li><b>Not claimed:</b> that this is a governance suite, that we hold enterprise
    certifications, or that we replace anything you run for access control. We are the
    spend half.</li>
    <li><b>Not claimed:</b> traction. We are early, and you can check the live surfaces
    yourself rather than take a metric on faith.</li>
  </ul>
  <h2>Check it rather than believe it</h2>
  <p>Agents find it at <a href="/llms.txt">/llms.txt</a> and
  <a href="/.well-known/x402.json">/.well-known/x402.json</a>. Humans can read
  <a href="/agent-ledger">the AgentLedger page</a>, watch a call get blocked in
  <a href="/demo">the demo</a>, or start at <a href="/start">/start</a> — no signup, no card.</p>
  <div class="cta-row">
    <a class="btn btn-human" href="/agent-ledger">See AgentLedger →</a>
    <a class="btn btn-ghost" href="/compare">How we differ from trace viewers →</a>
    <a class="btn btn-agentb" href="/developers">I'm an agent — /developers →</a>
  </div>
  <footer class="site">
    <div><a href="/manifesto">/manifesto</a><a href="/products">/products</a><a href="/developers">/developers</a><a href="/compare">/compare</a><a href="/changelog">/changelog</a><a href="/llms.txt">/llms.txt</a><a href="/status">status</a><a href="/security">security</a></div>
    <div>AI Agent City · one site, two readers</div>
  </footer>
</section>
</div>"""


PAGE_PRODUCTS = """<div class="agent-surface">
  <div class="kicker"><span class="ra">// machine-readable</span> · what an agent sees on /products</div>
  <div class="term"><div class="thead">$ agent-view /products</div><pre>
<span class="k">products:</span>
  - id: agent-ledger · mcp: /mcp/ (12 tools) · rest: /v1/*
    pricing: {free: "≤3 agents", starter: "$19/mo", team: "$79/mo", enterprise: "custom"} · x402: "$0.01 = 24h Pro"
  - id: agent-watch · mcp: /mcp/agent-watch (8 tools) · status: live (MCP verified 2026-09-20; tool calls translated to REST by the city gateway)
  - id: perimeter-watch · mcp: /mcp/perimeter-watch (6 tools) · status: live (MCP verified 2026-09-20; tool calls translated to REST by the city gateway) · free_snapshot: https://entradox.github.io/perimeter-watch-site/
  - id: trustscan · mcp: /mcp/trustscan (4 tools · v4.0.3) · live
  - id: cited · mcp: /mcp/cited (9 tools) · status: live (MCP verified 2026-09-20; tool calls translated to REST by the city gateway) · free_scan: https://entradox.github.io/cited-site/
<span class="k">capabilities:</span> [spend-caps, monitoring, perimeter-scan, trust-scan, ai-visibility]
<span class="k">auth:</span> workspace_key (human) | X-PAYMENT x402 (agent, no human)
</pre></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>

<div class="human-surface">
  <div class="kicker"><span class="rh">// human-readable</span> · aiagentscity.com/products</div>
  <h1>One stack.<br>Five products.</h1>
  <p class="lede">Each solves one operational problem end to end. Each is <b>live, independently usable, and agent-callable</b> — MCP everywhere; REST + CLI on AgentLedger.</p>

  <div class="card hl">
    <div class="stacknum"><b>01</b> — CONTROL SPEND · FLAGSHIP <span class="ver">v0.4.1 · 12 tools</span></div>
    <h3>AgentLedger — spending limits for AI agents</h3>
    <p><b>Your agents spend money. Give each one a spending limit.</b> Meters every call, shows a live P&amp;L per agent, and refuses the call that would cross the budget — before the provider is contacted.</p>
    <ul class="feat">
      <li><b>Pre-provider enforcement</b> — the proxy estimates max cost and returns <b>402</b>. The provider never sees the request; the money is never spent.</li>
      <li><b>Cache-aware pricing</b> — real tokens × real model rates. Flat-rate was 7.4× wrong on a real session.</li>
      <li><b>Agents buy their own Pro</b> — $0.01 x402 on __NETWORK_LABEL__, 24h, zero human clicks.</li>
      <li><b>Push alerts + shareable reports</b> — webhooks with retries; signed, expiring report links.</li>
    </ul>
    <pre><button class="copybtn" onclick="copyPre(this)">copy</button>HTTP/1.1 <span class="k">402</span> Payment Required
{<span class="s">"error"</span>:{<span class="s">"type"</span>:<span class="s">"budget_error"</span>, <span class="s">"message"</span>:<span class="s">"blocked before the provider"</span>}}</pre>
    <p style="font-size:14px"><b style="color:var(--txt)">Free:</b> 3 agents, every rail, enforced caps. <b style="color:var(--txt)">Paid: from $19/mo</b> — Starter (10 agents), Team (50), Enterprise custom.</p>
    <div class="cta-row"><a class="btn btn-human" href="/start">Get a workspace — no signup, no card</a><a class="btn btn-ghost" href="/agent-ledger#pricing">Pricing →</a><a class="btn btn-agentb" href="/developers">$ mcp add agent-ledger →</a></div>
  </div>

  <div class="card">
    <div class="stacknum"><b>02</b> — MONITOR <span class="ver">v1.30.0 · 8 MCP tools</span></div>
    <h3>Agent Watch — monitoring for the agent economy</h3>
    <p><b>Non-human traffic is hitting your APIs and you can't see it.</b> What changed this week, alerts the moment a new agent calls your endpoints, anomaly summaries — before they become incidents or invoices.</p>
    <pre><button class="copybtn" onclick="copyPre(this)">copy</button><span class="c">"Alert me when a new agent calls our API, with a weekly activity summary."</span></pre>
    <p class="mut" style="font-size:13px">Status: <b>live</b> — MCP handshake and all 8 tools verified working (2026-09-20). Tool calls execute through the city gateway, which translates them to the product's documented REST API. <a href="/status">Live status →</a></p>
    <div class="cta-row"><a class="btn btn-human" href="/agent-watch">Start monitoring</a><a class="btn btn-ghost" href="/agent-watch#pricing">Pricing →</a><a class="btn btn-agentb" href="/developers">$ mcp add agent-watch →</a></div>
  </div>

  <div class="card">
    <div class="stacknum"><b>03</b> — SECURE <span class="ver">v1.30.0 · 6 MCP tools</span></div>
    <h3>Perimeter Watch — passive external-perimeter monitoring for web agencies</h3>
    <p><b>Attackers view your clients' domains from the outside. So do we.</b> Dangling DNS, expiring certs, lookalike domains — scanned passively, briefed weekly.</p>
    <pre><button class="copybtn" onclick="copyPre(this)">copy</button><span class="c">"Scan example.com for dangling DNS and cert expiry, then check lookalikes."</span></pre>
    <p class="mut" style="font-size:13px">Status: <b>live</b> — MCP handshake and all 6 tools verified working (2026-09-20). Tool calls execute through the city gateway, which translates them to the product's documented REST API. The free browser snapshot works today. <a href="/status">Live status →</a></p>
    <div class="cta-row"><a class="btn btn-human" href="/perimeter-watch">Scan a domain</a><a class="btn btn-ghost" href="/perimeter-watch#pricing">Pricing →</a><a class="btn btn-agentb" href="/developers">$ mcp add perimeter-watch →</a></div>
  </div>

  <div class="card">
    <div class="stacknum"><b>04</b> — TRUST <span class="ver">v4.0.3 · 4 MCP tools · live</span></div>
    <h3>TrustScan — scan before you trust</h3>
    <p><b>Your agent is about to install a stranger's MCP server.</b> TrustScan answers the question every agent should ask first: <i>is this safe to wire in?</i> Invisible-Unicode prompt-injection, dangerous code patterns (MCP001&ndash;MCP006), hardcoded secrets, typosquat names — a 0&ndash;100 score, a letter grade, and evidence. Live over MCP today, read-only, no signup.</p>
    <div class="cta-row"><a class="btn btn-human" href="/trust-scan">Scan a server</a><a class="btn btn-ghost" href="/trust-scan#pricing">Pricing →</a><a class="btn btn-agentb" href="/developers">$ mcp add trustscan →</a></div>
  </div>

  <div class="card">
    <div class="stacknum"><b>05</b> — BE FOUND <span class="ver">v1.30.0 · 9 MCP tools</span></div>
    <h3>Cited — does AI recommend your practice?</h3>
    <p><b>Your customers stopped Googling. They ask AI.</b> Instant scan of what the AI engines say about your business — with verbatim evidence. Free.</p>
    <pre><button class="copybtn" onclick="copyPre(this)">copy</button><span class="c">"Scan Gentry Dentistry of Suwanee for AI visibility — verbatim quotes."</span></pre>
    <p class="mut" style="font-size:13px">Status: <b>live</b> — MCP handshake and all 9 tools verified working (2026-09-20). Tool calls execute through the city gateway, which translates them to the product's documented REST API. The free browser scan works today. <a href="/status">Live status →</a></p>
    <div class="cta-row"><a class="btn btn-human" href="/cited">Run your free scan</a><a class="btn btn-ghost" href="/cited#pricing">Pricing →</a><a class="btn btn-agentb" href="/developers">$ mcp add cited →</a></div>
  </div>

  <div class="pull">Five products, one thesis: the agent economy needs operations. We're building the boring infrastructure that makes the exciting future possible.</div>
  <footer class="site"><div><a href="/">← /</a><a href="/developers">/developers</a></div><div>AI Agent City</div></footer>
</div>"""

PAGE_DEVELOPERS = """<div class="agent-surface">
  <div class="kicker"><span class="ra">// machine-readable</span> · what an agent sees on /developers</div>
  <div class="term"><div class="thead">$ agent-view /developers</div><pre>
<span class="k">discovery:</span> /.well-known/x402.json · /llms.txt · /skill.md
<span class="k">mcp:</span>
  agent-ledger: https://aiagentscity.com/mcp/  <span class="c"># 12 tools</span>
  agent-watch: .../mcp/agent-watch            <span class="c"># 8 tools · live: tool calls translated to REST by the city gateway</span>
  perimeter-watch: .../mcp/perimeter-watch    <span class="c"># 6 tools · live: tool calls translated to REST by the city gateway</span>
  cited: .../mcp/cited                        <span class="c"># 9 tools · live: tool calls translated to REST by the city gateway</span>
  trustscan: .../mcp/trustscan                <span class="c"># 4 tools · live</span>
<span class="k">purchase:</span>
  POST /v1/billing/x402 · header X-PAYMENT=&lt;signed&gt;
  price: $0.01 USDC · chain: __NETWORK__ · grants: 24h Pro
<span class="k">rest:</span> /openapi.json · /server.json · AL-API-Version: 2026-09-01 (required on writes)
</pre></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>

<div class="human-surface">
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
    <tr><td><b>agent-ledger</b> <span class="ver">12 tools</span></td><td style="color:var(--mut)">Track spend, set budgets, pull P&amp;L, manage alerts</td><td><code style="font-family:var(--mono);font-size:12px">…/mcp/</code></td></tr>
    <tr><td><b>agent-watch</b> <span class="ver">8 tools</span></td><td style="color:var(--mut)">Activity briefs, new-agent alerts, anomaly summaries</td><td><code style="font-family:var(--mono);font-size:12px">…/mcp/agent-watch</code></td></tr>
    <tr><td><b>perimeter-watch</b> <span class="ver">6 tools</span></td><td style="color:var(--mut)">Dangling-DNS scans, cert watch, lookalike checks</td><td><code style="font-family:var(--mono);font-size:12px">…/mcp/perimeter-watch</code></td></tr>
    <tr><td><b>cited</b> <span class="ver">9 tools</span></td><td style="color:var(--mut)">AI-visibility scans with verbatim evidence</td><td><code style="font-family:var(--mono);font-size:12px">…/mcp/cited</code></td></tr>
    <tr><td><b>trustscan</b> <span class="ver">4 tools · v4.0.3 · live</span></td><td style="color:var(--mut)">MCP-server/skill security scans (prompt-injection, secrets, typosquat)</td><td><code style="font-family:var(--mono);font-size:12px">…/mcp/trustscan</code></td></tr>
  </table>
  <pre><button class="copybtn" onclick="copyPre(this)">copy</button><span class="c"># one command and your agent is connected</span>
claude mcp add --transport http agent-ledger https://aiagentscity.com/mcp/</pre>

  <h2>Checkout without humans</h2>
  <div class="card" style="border-color:var(--agent-dim)">
    <h3>$0.01. __NETWORK_LABEL__. Zero clicks.</h3>
    <p>An agent with a wallet buys 24h of AgentLedger Pro — unlimited agents — on the workspace its wallet resolves to. Real USDC on <b>__NETWORK__</b>. No signup flow, no card form, no human.</p>
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
</div>"""

PAGE_COMPARE = """<div class="agent-surface">
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
  price:           {agentledger: "from $19/mo", langsmith: "$39/seat",
                    helicone: "$79/mo", braintrust: "$249/mo", openrouter: "usage"}
  prices_verified: "2026-09-19"
</pre></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>

<div class="human-surface">
  <div class="kicker"><span class="rh">// human-readable</span> · aiagentscity.com/compare · prices verified 2026-09-19</div>
  <h1>Built to enforce.<br>Not to report.</h1>
  <p class="lede">Trace viewers are excellent at answering <i>"what did my agent do?"</i> — after you've paid for it. AgentLedger answers a different question: <i>"stop the call that breaks the budget."</i> Here's the honest map, including where the competition wins.</p>
  <table class="cmp">
    <tr><th></th><th><span class="us">AgentLedger</span></th><th>LangSmith</th><th>Helicone</th><th>Braintrust</th><th>OpenRouter</th></tr>
    <tr><td><b>Stops the overspend</b></td><td class="y"><b>Yes — 402 before provider contact</b></td><td class="part">Yes — gateway spend policies, 402<span class="note">their gateway traffic only</span></td><td class="part">Spend-based rate limits<span class="note">throttles, not budgets</span></td><td class="n">No — cost regression tracking</td><td class="part">Per-key spend limits<span class="note">OpenRouter-routed traffic only</span></td></tr>
    <tr><td><b>Budgets belong to the agent</b></td><td class="y"><b>Yes — keyed to agent_id</b></td><td class="n">Org / key / user</td><td class="n">No</td><td class="n">No</td><td class="n">API keys</td></tr>
    <tr><td><b>Agent buys itself</b></td><td class="y"><b>$0.01 x402, no human</b></td><td class="n">No</td><td class="n">No</td><td class="n">No</td><td class="n">No</td></tr>
    <tr><td><b>Trace debugging</b></td><td class="n">No — by design</td><td class="y">Best in class</td><td class="y">Yes</td><td class="y">Eval-first</td><td class="n">No</td></tr>
    <tr><td><b>Price</b></td><td class="y"><b>from $19/mo</b></td><td class="n">$39/seat/mo</td><td class="n">$79/mo</td><td class="n">$249/mo</td><td class="n">usage-based</td></tr>
  </table>
  <div class="pull" style="font-size:19px">If you want to <i>understand</i> your spend, buy a trace viewer — LangSmith's is superb. If you want to <i>control</i> it, per agent, with the agent itself as the customer — that's us.</div>
  <div class="card"><h3>Also in the space</h3><p class="mut"><b>LiteLLM</b>, <b>Portkey</b>, <b>LangDB</b> — gateway proxies with per-key spend controls, scoped to the traffic routed through them. <b>Revenium</b> — API metering for monetization. <b>Langfuse</b> — open-source trace observability. Gateway- and trace-layer tools see their own layer; AgentLedger enforces per-agent budgets with a 402 before any provider is contacted, and the agent itself can buy Pro.</p></div>
  <div class="cta-row"><a class="btn btn-human" href="/demo">Try the enforcement live — no signup</a></div>
  <footer class="site"><div><a href="/">← /</a><a href="/products">/products</a></div><div>AI Agent City</div></footer>
</div>"""

PAGE_CHANGELOG = """<div class="agent-surface">
  <div class="kicker"><span class="ra">// machine-readable</span> · what an agent sees on /changelog</div>
  <div class="term"><div class="thead">$ agent-view /changelog</div><pre>
<span class="k">releases:</span>
  - 2026-09-20: MCP endpoints verified end-to-end (handshake + tools/list) for all
    five products — ledger 12, watch 8, perimeter 6, trustscan 4, cited 9; the
    earlier "native MCP dispatch being repaired" notes were retired because the
    gateway path every client uses works today
  - 2026-09-19: satellite tool calls execute via the city gateway (MCP tools/call
    translated to each product's documented REST API); watch (8 tools),
    perimeter (6), cited (9) all callable
  - 2026-09-16: x402 live on __NETWORK__ ($0.01 USDC = 24h Pro, zero-click)
  - 2026-09-16: agent-ledger fixes (agent_secret format, model alias pricing,
    dashboard webhooks, typed payment_required errors)
  - 2026-09-16: per-product releases (39 MCP tools: ledger 12, watch 8, perimeter 6, cited 9, trustscan 4)
<span class="k">feed:</span> /changelog (this page) · /status (live)
</pre></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>

<div class="human-surface">
  <div class="kicker"><span class="rh">// human-readable</span> · aiagentscity.com/changelog</div>
  <h1>Shipping fast.</h1>
  <p class="lede">Velocity is the pitch. Every ship, dated — the proof the stack is alive.</p>
  <div class="chlog">
    <div class="e"><div class="d">2026-09-19</div><div class="t"><b>Satellite MCP endpoints mounted on aiagentscity.com.</b> The documented <span class="ver">/mcp/agent-watch</span>, <span class="ver">/mcp/perimeter-watch</span>, <span class="ver">/mcp/cited</span> and <span class="ver">/mcp/trustscan</span> routes now resolve to the owning backends instead of 404ing; TrustScan is fully wired (4 tools, live). Agent Watch, Perimeter Watch and Cited list tools correctly via the city gateway. <span class="ver">platform</span></div></div>
    <div class="e"><div class="d">2026-09-19</div><div class="t"><b>Satellite tool calls now execute via the city gateway.</b> The three v1.30.0 backends time out every native MCP <span class="ver">tools/call</span> server-side, so the gateway translates tool calls to each product's documented REST API and returns proper MCP results: Agent Watch (8 tools), Perimeter Watch (6 tools), Cited (9 tools) all callable today. <span class="ver">platform</span></div></div>
    <div class="e"><div class="d">2026-09-16</div><div class="t"><b>x402 live on __NETWORK_LABEL__.</b> $0.01 USDC → 24h of AgentLedger Pro. Agents buy with zero human clicks — the first purchase completed end-to-end. <span class="ver">agent-ledger</span></div></div>
    <div class="e"><div class="d">2026-09-16</div><div class="t"><b>Four fixes from live testing.</b> Real <span class="ver">agent_secret="as_…"</span> format in the docs, SDK model IDs auto-priced as aliases, webhooks section on the dashboard, typed <span class="ver">payment_required</span> errors on the x402 endpoint. <span class="ver">agent-ledger</span></div></div>
    <div class="e"><div class="d">2026-09-16</div><div class="t"><b>Per-product releases.</b> AgentLedger <span class="ver">v0.4.1</span> (12 MCP tools) · Agent Watch <span class="ver">v1.30.0</span> (8) · Perimeter Watch <span class="ver">v1.30.0</span> (6) · Cited <span class="ver">v1.30.0</span> (9) · TrustScan <span class="ver">v4.0.3</span> (4). 39 MCP tools across five products. <span class="ver">platform</span></div></div>
  </div>
  <div class="cta-row"><a class="btn btn-ghost" href="/status">/status — live system status →</a></div>
  <footer class="site"><div><a href="/">← /</a></div><div>AI Agent City</div></footer>
</div>"""

PAGES_META = {
    "/": ("PAGE_HOME", "AI Agent City", "AI Agent City is the operations layer for the agent economy: spending limits, monitoring, security, trust, and discovery for AI agents. Human-readable and machine-readable."),
    "/manifesto": ("PAGE_MANIFESTO", "Access vs spend — AI Agent City", "Kiteworks governs what agents can touch. AgentLedger governs what agents can spend. Two halves of one problem, and only one of them has an owner."),
    "/products": ("PAGE_PRODUCTS", "Products — AI Agent City", "Five products, one thesis: the agent economy needs operations. AgentLedger, Agent Watch, Perimeter Watch, TrustScan, and Cited — each live, independently usable, and agent-callable."),
    "/developers": ("PAGE_DEVELOPERS", "Developers — AI Agent City", "MCP, REST, CLI, and the x402 purchase path. Machine-readable surfaces for all five products."),
    "/compare": ("PAGE_COMPARE", "AI Agent City vs trace viewers — AI Agent City", "How per-agent budget enforcement differs from request-level trace observability, with honestly dated list prices."),
    "/changelog": ("PAGE_CHANGELOG", "Changelog — AI Agent City", "External-facing product releases across the AI Agent City suite. Human-readable and machine-readable."),
}
