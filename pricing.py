"""Central pricing catalog for AI Agent City's five products.

Each product has a free tier (the funnel) and paid tiers. Paid CTAs resolve
to Stripe Payment Links configured via environment variables; when a link is
not configured the CTA falls back to the product's free entry point so a
pricing card can never render a dead button. Setting the env var flips the
button to checkout with no code change.

Railway env vars (create one Payment Link per tier in the Stripe dashboard):

    AgentLedger (fulfillment is automatic via the Stripe webhook)
      AL_STRIPE_PAYMENT_LINK   $19/mo Starter  (already set — the live link)
      AL_STRIPE_STARTER_ANNUAL $190/yr Starter annual
      AL_STRIPE_TEAM_LINK      $79/mo Team
      AL_STRIPE_TEAM_ANNUAL    $790/yr Team annual
    Agent Watch (manual fulfillment until the satellite bills itself)
      AW_STRIPE_WATCH_LINK / AW_STRIPE_WATCH_ANNUAL       $29/mo / $290/yr
      AW_STRIPE_PLUS_LINK  / AW_STRIPE_PLUS_ANNUAL        $79/mo / $790/yr
    Perimeter Watch (manual fulfillment; satellite already sells $9/$19)
      PW_STRIPE_LITE_LINK / PW_STRIPE_LITE_ANNUAL        $29/mo / $290/yr
      PW_STRIPE_AGENCY_LINK / PW_STRIPE_AGENCY_ANNUAL    $99/mo / $990/yr
    TrustScan (manual fulfillment)
      TS_STRIPE_TEAM_LINK / TS_STRIPE_TEAM_ANNUAL        $49/mo / $490/yr
    Cited (manual fulfillment)
      CT_STRIPE_REPORT_LINK                              $49 one-time
      CT_STRIPE_MONITOR_LINK / CT_STRIPE_MONITOR_ANNUAL  $149/mo / $1490/yr

Annual links are optional: when unset, the annual toggle still shows the
2-months-free price but the CTA uses the monthly link.
"""

import os

# ---------------------------------------------------------------------------
# Stripe link resolution
# ---------------------------------------------------------------------------

def _link(*names: str) -> str:
    for n in names:
        v = (os.environ.get(n) or "").strip()
        if v:
            return v
    return ""


# ---------------------------------------------------------------------------
# Tier catalog. price_mo/price_yr in dollars; price_yr is ~10x (2 months free).
# kind: "sub" (monthly/annual toggle), "once" (one-time, no toggle),
#       "custom" (no checkout link by design).
# link: env-var name holding the Stripe Payment Link (monthly or one-time).
# link_annual: env-var name for the annual Payment Link (optional).
# fallback: where the CTA goes when no link is configured (free entry).
# ---------------------------------------------------------------------------

PRODUCTS = {
    "agent-watch": {
        "name": "Agent Watch",
        "free_entry": "#probe",
        "tiers": [
            {"name": "Free", "kind": "free", "price_mo": 0,
             "blurb": "The funnel. Free forever.",
             "features": ["Probe any endpoint — liveness, latency, price challenge",
                          "Live agent-economy census",
                          "All 8 MCP tools"],
             "cta": "Probe free", "href": "#probe"},
            {"name": "Watch", "kind": "sub", "price_mo": 29, "price_yr": 290,
             "blurb": "For teams shipping agent-facing APIs.",
             "features": ["10 monitored endpoints",
                          "Email alerts the moment a new agent calls",
                          "Weekly activity summary",
                          "Watchlist dashboard"],
             "cta": "Get Watch", "link": "AW_STRIPE_WATCH_LINK",
             "link_annual": "AW_STRIPE_WATCH_ANNUAL"},
            {"name": "Watch Plus", "kind": "sub", "price_mo": 79, "price_yr": 790,
             "blurb": "For platforms with real agent traffic.",
             "features": ["50 monitored endpoints",
                          "Everything in Watch",
                          "API access to watchlist + alerts",
                          "Priority probe frequency"],
             "cta": "Get Watch Plus", "link": "AW_STRIPE_PLUS_LINK",
             "link_annual": "AW_STRIPE_PLUS_ANNUAL"},
        ],
    },
    "perimeter-watch": {
        "name": "Perimeter Watch",
        "free_entry": "https://entradox.github.io/perimeter-watch-site/",
        "tiers": [
            {"name": "Free", "kind": "free", "price_mo": 0,
             "blurb": "One-time snapshot. Free forever.",
             "features": ["One full perimeter snapshot",
                          "Dangling DNS, cert expiry, lookalike domains",
                          "No signup"],
             "cta": "Run a free snapshot",
             "href": "https://entradox.github.io/perimeter-watch-site/"},
            {"name": "Lite", "kind": "sub", "price_mo": 29, "price_yr": 290,
             "blurb": "For solo devs and small shops.",
             "features": ["5 domains",
                          "Weekly perimeter brief by email",
                          "Dangling DNS + cert + lookalike monitoring"],
             "cta": "Get Lite", "link": "PW_STRIPE_LITE_LINK",
             "link_annual": "PW_STRIPE_LITE_ANNUAL"},
            {"name": "Agency", "kind": "sub", "price_mo": 99, "price_yr": 990,
             "blurb": "For web agencies reselling monitoring.",
             "features": ["25 domains",
                          "Everything in Lite",
                          "White-label client brief PDFs",
                          "Priority re-scans"],
             "cta": "Get Agency", "link": "PW_STRIPE_AGENCY_LINK",
             "link_annual": "PW_STRIPE_AGENCY_ANNUAL"},
        ],
    },
    "trust-scan": {
        "name": "TrustScan",
        "free_entry": "#npmcheck",
        "tiers": [
            {"name": "Free", "kind": "free", "price_mo": 0,
             "blurb": "Scan before you trust. Free forever.",
             "features": ["Unlimited npm package checks",
                          "Full MCP deep scans",
                          "0–100 TrustScore + letter grade"],
             "cta": "Scan free", "href": "#npmcheck"},
            {"name": "Team", "kind": "sub", "price_mo": 49, "price_yr": 490,
             "blurb": "For teams installing third-party agent code.",
             "features": ["Scan on every deploy (CI integration)",
                          "Scan history + trend per package",
                          "Policy gates — block deploys under a grade",
                          "Shared team dashboard"],
             "cta": "Get Team", "link": "TS_STRIPE_TEAM_LINK",
             "link_annual": "TS_STRIPE_TEAM_ANNUAL"},
            {"name": "Enterprise", "kind": "custom", "price_mo": 0,
             "blurb": "For private registries and regulated shops.",
             "features": ["Private MCP registry scanning",
                          "Custom policy packs",
                          "SLA + dedicated support"],
             "cta": "Start with Team", "link": "TS_STRIPE_TEAM_LINK"},
        ],
    },
    "cited": {
        "name": "Cited",
        "free_entry": "https://entradox.github.io/cited-site/",
        "tiers": [
            {"name": "Free", "kind": "free", "price_mo": 0,
             "blurb": "One engine, one prompt. Free forever.",
             "features": ["Single AI-visibility scan",
                          "Verbatim engine quotes",
                          "No signup"],
             "cta": "Run a free scan",
             "href": "https://entradox.github.io/cited-site/"},
            {"name": "Full Report", "kind": "once", "price_once": 49,
             "blurb": "The complete picture, one time.",
             "features": ["Multi-engine scan (not one model)",
                          "Multi-prompt coverage",
                          "Head-to-head vs two competitors",
                          "Shareable report link"],
             "cta": "Get the full report", "link": "CT_STRIPE_REPORT_LINK"},
            {"name": "Monitoring", "kind": "sub", "price_mo": 149, "price_yr": 1490,
             "blurb": "For practices where being recommended is revenue.",
             "features": ["Weekly AI-visibility scans",
                          "Alerts when a competitor overtakes you",
                          "Prompt-level trend tracking",
                          "Monthly executive brief"],
             "cta": "Get Monitoring", "link": "CT_STRIPE_MONITOR_LINK",
             "link_annual": "CT_STRIPE_MONITOR_ANNUAL"},
        ],
    },
}

# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

PRICING_CSS = """
.pz-wrap{{max-width:960px;margin:0 auto}}
.pz-head{{display:flex;align-items:baseline;justify-content:space-between;gap:12px;flex-wrap:wrap;margin:0 0 6px}}
.pz-head h2{{font-family:Georgia,'Times New Roman',serif;font-size:26px;font-weight:600;margin:0}}
.pz-toggle{{display:flex;align-items:center;gap:8px;font-size:13px;color:#6b7280}}
.pz-toggle button{{border:1px solid #e2e5ea;background:#fff;border-radius:999px;padding:6px 14px;font-size:13px;cursor:pointer;color:#374151}}
.pz-toggle button.on{{background:#1a1a2e;color:#fff;border-color:#1a1a2e}}
.pz-grid{{display:grid;grid-template-columns:repeat(3,1fr);gap:14px;margin-top:14px}}
.pz-card{{background:#fff;border:1px solid #e7e4dc;border-radius:14px;padding:20px;display:flex;flex-direction:column}}
.pz-card.hot{{border:2px solid #4f46e5;box-shadow:0 8px 24px rgba(79,70,229,.10)}}
.pz-name{{font-size:13px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;color:#6b7280}}
.pz-price{{font-family:Georgia,'Times New Roman',serif;font-size:34px;margin:8px 0 2px;color:#1a1a2e}}
.pz-price small{{font-size:14px;font-family:-apple-system,'Segoe UI',sans-serif;color:#6b7280;font-weight:400}}
.pz-blurb{{font-size:13px;color:#6b7280;margin:0 0 12px}}
.pz-card ul{{list-style:none;margin:0 0 16px;padding:0;font-size:13.5px;line-height:1.5}}
.pz-card li{{padding:5px 0 5px 20px;position:relative;color:#374151}}
.pz-card li:before{{content:"✓";position:absolute;left:0;color:#4f46e5;font-weight:700}}
.pz-cta{{margin-top:auto;display:block;text-align:center;background:#4f46e5;color:#fff !important;font-weight:600;font-size:14px;padding:11px 16px;border-radius:9px;text-decoration:none}}
.pz-cta:hover{{background:#4338ca}}
.pz-cta.ghost{{background:#fff;color:#1a1a2e !important;border:1px solid #d8d4c8}}
.pz-cta.ghost:hover{{background:#faf9f6}}
.pz-note{{font-size:12px;color:#9aa0ae;margin-top:10px;text-align:center}}
@media(max-width:720px){{.pz-grid{{grid-template-columns:1fr}}}}
"""

_TOGGLE_JS = """
<script>
function pzToggle(pid, annual){
  var wrap = document.getElementById('pz-'+pid);
  if(!wrap) return;
  wrap.querySelectorAll('.pz-toggle button').forEach(function(b){
    b.classList.toggle('on', b.dataset.per === (annual ? 'yr' : 'mo'));
  });
  wrap.querySelectorAll('[data-mo]').forEach(function(el){
    var mo = el.dataset.mo, yr = el.dataset.yr, per = el.dataset.per;
    if(el.classList.contains('pz-price')){
      el.innerHTML = (annual ? yr : mo) + '<small>/' + (annual ? 'yr' : per) + '</small>';
    } else if(el.classList.contains('pz-cta')){
      var href = annual ? (el.dataset.hrefYr || el.dataset.hrefMo) : el.dataset.hrefMo;
      if(href) el.setAttribute('href', href);
    }
  });
  var note = wrap.querySelector('.pz-save');
  if(note) note.style.display = annual ? 'inline' : 'none';
}
</script>
"""


def _cta(tier: dict, free_entry: str) -> tuple:
    """(href, label, configured). Never returns an empty href."""
    link = _link(tier.get("link", "")) if tier.get("link") else ""
    if link:
        return link, tier["cta"], True
    href = tier.get("href") or free_entry
    if tier.get("kind") in ("free", "custom"):
        label = tier["cta"]
    else:
        label = "Start free"
    return href, label, False


def pricing_section(product_id: str) -> str:
    """Full pricing section HTML (anchor #pricing) for a satellite product."""
    prod = PRODUCTS[product_id]
    free_entry = prod["free_entry"]
    has_sub = any(t["kind"] == "sub" for t in prod["tiers"])
    cards = []
    for i, t in enumerate(prod["tiers"]):
        kind = t["kind"]
        if kind == "free":
            price = '$0<small> forever</small>'
            data = ''
        elif kind == "once":
            price = f'${t["price_once"]}<small> one-time</small>'
            data = ''
        elif kind == "custom":
            price = 'Custom'
            data = ''
        else:  # sub — monthly/annual toggle swaps these
            price = f'${t["price_mo"]}<small>/mo</small>'
            data = (f' data-mo="${t["price_mo"]}" data-yr="${t["price_yr"]}"'
                    f' data-per="mo"')
        href, label, configured = _cta(t, free_entry)
        if kind == "sub":
            annual_href = _link(t.get("link_annual", "")) if t.get("link_annual") else ""
            cta = (f'<a class="pz-cta{" ghost" if not configured else ""}"'
                   f' data-mo="1" data-href-mo="{href}" data-href-yr="{annual_href}"'
                   f' href="{href}">{label}</a>')
        elif kind == "custom":
            cta = f'<a class="pz-cta ghost" href="{href}">{label}</a>'
        else:
            ghost = " ghost" if kind == "free" else ""
            cta = f'<a class="pz-cta{ghost}" href="{href}">{label}</a>'
        feats = "".join(f"<li>{f}</li>" for f in t["features"])
        hot = " hot" if configured and kind == "sub" and i == 1 else ""
        cards.append(
            f'<div class="pz-card{hot}"><div class="pz-name">{t["name"]}</div>'
            f'<div class="pz-price"{data}>{price}</div>'
            f'<p class="pz-blurb">{t["blurb"]}</p><ul>{feats}</ul>{cta}</div>')
    toggle = ""
    if has_sub:
        toggle = (f'<div class="pz-toggle"><button class="on" data-per="mo" '
                  f'onclick="pzToggle(\'{product_id}\',false)">Monthly</button>'
                  f'<button data-per="yr" onclick="pzToggle(\'{product_id}\',true)">'
                  f'Annual</button><span class="pz-save" style="display:none">'
                  f'· 2 months free</span></div>')
    return (
        f'<div class="pz-wrap" id="pz-{product_id}">'
        f'<div class="pz-head" id="pricing"><h2>Pricing</h2>{toggle}</div>'
        f'<div class="pz-grid">{"".join(cards)}</div>'
        f'<p class="pz-note">Prices in USD. Cancel any time — subscriptions '
        f'end at the close of the billing period, no questions, no retention flow.'
        f'</p></div>' + _TOGGLE_JS)
