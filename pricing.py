"""Central pricing catalog for AI Agent City's products.

Blueprint v2 (2026-10-11): the customer is an agent and the only paid path on
the domain is x402. Human paid tiers ($19/$79 Stripe checkout plans) were
removed — the catalog now carries each product's free tier plus, where a real
agent-paid rail exists, the rail as data (`rail`). A product with no live rail
shows no paid card at all: advertising a price no endpoint can charge is a
false claim, and on a trust product it is self-contradiction.

The old Stripe Payment Link env vars (AL_STRIPE_PAYMENT_LINK, *_TEAM_LINK,
*_ANNUAL, and the per-satellite links) are no longer read anywhere in this
module; the env vars may still exist on the deployment but do nothing here.
"""


# ---------------------------------------------------------------------------
# Tier catalog (blueprint v2, 2026-10-11). Only "free" tiers remain — the
# human paid tiers were removed with the Stripe door. A product with a real
# agent-paid rail marks it in `rail`; the pass price/duration are derived
# from x402_verify at render time so the page can never drift from the offer
# the endpoint actually makes.
# ---------------------------------------------------------------------------

PRODUCTS = {
    "agent-watch": {
        "name": "Agent Watch",
        "free_entry": "#probe",
        "tiers": [
            {"name": "Free", "kind": "free",
             "blurb": "The funnel. Free forever.",
             "features": ["Probe any endpoint — liveness, latency, price challenge",
                          "Live agent-economy census",
                          "All 8 MCP tools"],
             "cta": "Probe free", "href": "#probe"},
        ],
    },
    "perimeter-watch": {
        "name": "Perimeter Watch",
        "free_entry": "https://entradox.github.io/perimeter-watch-site/",
        "tiers": [
            {"name": "Free", "kind": "free",
             "blurb": "One-time snapshot. Free forever.",
             "features": ["One full perimeter snapshot",
                          "Dangling DNS, cert expiry, lookalike domains",
                          "No signup"],
             "cta": "Run a free snapshot",
             "href": "https://entradox.github.io/perimeter-watch-site/"},
        ],
    },
    "trust-scan": {
        "name": "TrustScan",
        "free_entry": "#npmcheck",
        "tiers": [
            {"name": "Free", "kind": "free",
             "blurb": "Scan before you trust. Free forever.",
             "features": ["Unlimited npm package checks",
                          "Full MCP deep scans",
                          "0–100 TrustScore + letter grade"],
             "cta": "Scan free", "href": "#npmcheck"},
        ],
    },
    "cited": {
        "name": "Cited",
        "free_entry": "https://entradox.github.io/cited-site/",
        "tiers": [
            {"name": "Free", "kind": "free",
             "blurb": "One engine, one prompt. Free forever.",
             "features": ["Single AI-visibility scan",
                          "Verbatim engine quotes",
                          "No signup"],
             "cta": "Run a free scan",
             "href": "https://entradox.github.io/cited-site/"},
        ],
    },
    "workbench": {
        "name": "AI Workbench",
        "free_entry": "/workbench",
        "tiers": [
            {"name": "Free", "kind": "free",
             "blurb": "The audit trail for agent work. Free forever.",
             "features": ["npx @aiagentscity/setup workbench — one command",
                          "Scoped write gates on 6 agent harnesses",
                          "Frame cards + machine-witnessed gate ledger",
                          "Close receipts, verifiable by anyone",
                          "Local-only: nothing leaves the machine"],
             "cta": "Install free", "href": "/workbench"},
        ],
    },
    "agent-ledger": {
        "name": "AgentLedger",
        "free_entry": "/start",
        "rail": True,  # the city's only live paid rail — see _rail_card()
        "tiers": [
            {"name": "Free", "kind": "free",
             "blurb": "Up to 3 agents per workspace, every rail, no expiry.",
             "features": ["3 tracked agents per workspace",
                          "Every rail: mcp, x402, api_key, manual",
                          "Budget caps, enforced pre-provider",
                          "Full ledger + CSV export"],
             "cta": "Get a workspace — no signup, no card", "href": "/start"},
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

def _rail_card() -> str:
    """The agent-paid rail card — rendered from the live x402 config so the
    advertised offer can never drift from the price the endpoint charges."""
    import x402_verify
    hours = round(x402_verify.X402_PRO_PASS_SECONDS / 3600)
    return (
        '<div class="pz-card hot"><div class="pz-name">Pro pass — agent rail</div>'
        f'<div class="pz-price">{x402_verify.X402_MINT_PRICE}'
        f'<small> USDC / {hours}h</small></div>'
        '<p class="pz-blurb">Bought by the agent itself over x402 — no account, '
        'card form, or human checkout. Renewable: every payment buys a fresh '
        f'{hours}h window.</p>'
        f'<ul><li>Full Pro — {x402_verify.x402_pass_agent_limit()} on the wallet\'s workspace</li>'
        f'<li>Network: <code>{{X402_NETWORK}}</code>, paid in USDC</li>'
        '<li>POST /v1/billing/x402 with a signed payment header</li></ul>'
        '<a class="pz-cta" href="/.well-known/x402.json">'
        'Payment spec →</a></div>')


def pricing_section(product_id: str) -> str:
    """Pricing section HTML (anchor #pricing) for a product page.

    Blueprint v2: free tier cards plus, for products carrying a live agent
    rail, the rail card. No paid human tier is ever rendered — there is no
    checkout for one to point at.
    """
    prod = PRODUCTS[product_id]
    cards = []
    for t in prod["tiers"]:
        cta = f'<a class="pz-cta ghost" href="{t.get("href") or prod["free_entry"]}">{t["cta"]}</a>'
        feats = "".join(f"<li>{f}</li>" for f in t["features"])
        cards.append(
            f'<div class="pz-card"><div class="pz-name">{t["name"]}</div>'
            '<div class="pz-price">$0<small> forever</small></div>'
            f'<p class="pz-blurb">{t["blurb"]}</p><ul>{feats}</ul>{cta}</div>')
    if prod.get("rail"):
        cards.append(_rail_card())
    note = ('<p class="pz-note">Prices in USD. The only paid path on this '
            'domain is the agent rail (x402) — there is no human checkout. '
            'Free tiers never expire.</p>')
    return (
        f'<div class="pz-wrap" id="pz-{product_id}">'
        f'<div class="pz-head" id="pricing"><h2>Pricing</h2></div>'
        f'<div class="pz-grid">{"".join(cards)}</div>{note}</div>')


def pricing_md() -> str:
    """Machine-readable price sheet for /pricing.md.

    Rendered from PRODUCTS — the same source as the HTML /pricing page — so the
    two can never disagree. Blueprint v2: the only paid path is x402; this
    document names no human tier and no checkout link because none exist.
    """
    import x402_verify
    hours = round(x402_verify.X402_PRO_PASS_SECONDS / 3600)

    out = ["# AI Agent City — pricing", "",
           "Every product is free to verify and use. The only paid path on the "
           "domain is the agent rail (x402) — there is no human checkout.", ""]
    for prod in PRODUCTS.values():
        out.append(f"## {prod['name']}")
        out.append("")
        for t in prod["tiers"]:
            out.append(f"- **{t['name']} — $0** — {t['blurb']}")
            for feat in t["features"]:
                out.append(f"  - {feat}")
        if prod.get("rail"):
            out.append(f"- **Pro pass — {x402_verify.X402_MINT_PRICE} USDC / "
                       f"{hours}h** — bought by the agent itself via "
                       "POST /v1/billing/x402 (x402 `exact` payment). "
                       "Renewable: every payment buys a fresh window; the "
                       "paying wallet is the workspace identity.")
        out.append("")
    out += [
        "## Agent-paid rail (x402) — the only checkout",
        "",
        "An agent with a wallet buys a renewable "
        f"{hours}-hour AgentLedger Pro pass for "
        f"{x402_verify.X402_MINT_PRICE} USDC over x402 — no account, card form, "
        "or human. Request and response format: /.well-known/x402.json.",
        "",
        "## Terms",
        "",
        "Free tiers never expire. Pass purchases are final once settled — see "
        "/terms#refunds. Full pricing page: /pricing.",
        "",
    ]
    return "\n".join(out)
