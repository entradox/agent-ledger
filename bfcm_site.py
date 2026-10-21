"""BFCM Campaign-in-a-Box — sales page, sample pack, thanks page.

One product, one price ($499), one intake field (store URL). The checkout URL is
environment driven so the live payment link is configuration, not a code change.
The sample pack is rendered from content/bfcm-sample.md so the copy has a single
source of truth.
"""
from __future__ import annotations

import html as _html
import os
import re as _re

BFCM_CHECKOUT_URL = os.environ.get("BFCM_CHECKOUT_URL", "").strip()

SAMPLE_MD = os.path.join(os.path.dirname(os.path.abspath(__file__)), "content", "bfcm-sample.md")

PAGE_BFCM = """
<style>
  .bfcm-hero{padding:22px 0 8px}
  .bfcm-hero h1{max-width:20em}
  .bfcm-strip{display:flex;gap:26px;flex-wrap:wrap;margin:26px 0 0;padding:16px 0;
    border-top:1px solid var(--line-soft);border-bottom:1px solid var(--line-soft)}
  .bfcm-strip div{font-family:var(--mono);font-size:11px;letter-spacing:.08em;
    text-transform:uppercase;color:var(--faint)}
  .bfcm-strip b{display:block;font-family:var(--sans);font-size:19px;letter-spacing:-.01em;
    color:var(--ink);text-transform:none;margin-top:4px}
  .bfcm-grid{display:grid;grid-template-columns:1fr;gap:12px;margin-top:22px}
  @media(min-width:820px){.bfcm-grid{grid-template-columns:1fr 1fr}}
  .bfcm-num{font-family:var(--mono);font-size:11px;color:var(--accent-ink);
    letter-spacing:.18em;display:block;margin-bottom:8px}
  .bfcm-steps{counter-reset:s;margin-top:20px}
  .bfcm-step{display:flex;gap:16px;padding:18px 0;border-top:1px solid var(--line-soft)}
  .bfcm-step:last-child{border-bottom:1px solid var(--line-soft)}
  .bfcm-step .n{font-family:var(--mono);font-size:12px;color:var(--accent-ink);
    min-width:2.2em;padding-top:2px}
  .bfcm-step p{margin:0;color:var(--muted);font-size:14px;max-width:44em}
  .bfcm-step h3{margin:0 0 4px}
  .bfcm-table{width:100%;border-collapse:collapse;margin-top:18px;font-size:14px}
  .bfcm-table th,.bfcm-table td{text-align:left;padding:12px 10px;border-bottom:1px solid var(--line-soft)}
  .bfcm-table th{font-family:var(--mono);font-size:10.5px;letter-spacing:.14em;
    text-transform:uppercase;color:var(--faint);font-weight:400}
  .bfcm-table td:first-child{color:var(--muted)}
  .bfcm-table td:last-child{font-weight:600}
  .bfcm-table tr.us td{background:var(--accent-soft)}
  .bfcm-table tr.us td:last-child{color:var(--accent-deep)}
  .bfcm-faq details{border-top:1px solid var(--line-soft);padding:14px 0}
  .bfcm-faq details:last-of-type{border-bottom:1px solid var(--line-soft)}
  .bfcm-faq summary{cursor:pointer;font-weight:600;font-size:15px;list-style:none}
  .bfcm-faq summary::-webkit-details-marker{display:none}
  .bfcm-faq summary::after{content:"+";float:right;color:var(--faint);font-family:var(--mono)}
  .bfcm-faq details[open] summary::after{content:"-"}
  .bfcm-faq p{margin:10px 0 0;color:var(--muted);font-size:14px;max-width:46em}
  .bfcm-band{background:var(--accent-soft);border:1px solid var(--accent-line);
    border-radius:18px;padding:26px 22px;margin:26px 0}
  .bfcm-band h2{margin-bottom:8px}
  .bfcm-band p{color:var(--accent-deep);max-width:46em}
  .bfcm-sendlist{margin:14px 0 0;padding:0;list-style:none;
    font-family:var(--mono);font-size:12.5px;color:var(--muted)}
  .bfcm-sendlist li{padding:7px 0;border-bottom:1px solid var(--line-soft)}
  .bfcm-sendlist b{color:var(--ink);font-weight:600}
  .bfcm-sample section{border:0;padding:0}
  .bfcm-sample blockquote{border-left:2px solid var(--accent-line);padding:4px 0 4px 16px;
    margin:12px 0;color:var(--ink);font-size:15px}
  .bfcm-sample hr{border:0;border-top:1px solid var(--line-soft);margin:30px 0}
</style>

<div class="human-surface">
<div class="bfcm-hero">
  <div class="kicker"><span class="rh">// black friday 2026</span> · done for you · $499</div>
  <h1>Your Black Friday emails, written by Thursday.</h1>
  <p class="lede">Five emails, three texts, and three fixes to the checkout flow you already run. Written for <b>your</b> store from <b>your</b> URL: your products, your prices, your voice. You paste and send. Drafts in your inbox within 48 hours.</p>
  <div class="cta-row">
    <a class="btn btn-human" href="__CHECKOUT_URL__">Get my campaign — $499</a>
    <a class="btn btn-ghost" href="/bfcm/sample">Read the sample pack →</a>
  </div>
  <div class="bfcm-strip">
    <div>Written from<b>one field: your URL</b></div>
    <div>Delivered in<b>48 hours</b></div>
    <div>You send<b>drafts only</b></div>
    <div>Orders close<b>Nov 20</b></div>
  </div>
</div>

<section>
  <div class="kicker"><span class="rh">// what lands in your inbox</span></div>
  <h2>Eight sends and three fixes, ready to paste.</h2>
  <p class="lede">Not a template pack. A campaign written against your catalogue, aimed at the week that pays for the year.</p>
  <div class="bfcm-grid">
    <div class="card"><span class="bfcm-num">01 · EMAIL</span><h3>Five emails</h3><p>Subject lines with two alternates each, preview text, send time, the exact segment to hit, and the full body. Early access, the main offer, the gift angle, Cyber Monday, and the final hours.</p></div>
    <div class="card"><span class="bfcm-num">02 · SMS</span><h3>Three texts</h3><p>Under 160 characters, written for consent-safe lists, each with one link and one action. Friday morning, Saturday gift nudge, Monday final call.</p></div>
    <div class="card"><span class="bfcm-num">03 · CHECKOUT</span><h3>Three abandoned-checkout tweaks</h3><p>Timing, discount discipline, and one SMS rule: changes to the flow you already have in Shopify or Klaviyo. The highest-return edits in the pack.</p></div>
    <div class="card"><span class="bfcm-num">04 · OPS</span><h3>The calendar and the checklist</h3><p>A send calendar for Nov 24-30 and a seven-step setup sheet: merge tags, segments, suppressions, test sends. Written so one person can ship it in an afternoon.</p></div>
  </div>
</section>

<section>
  <div class="kicker"><span class="rh">// the sample</span></div>
  <h2>Read the whole pack before you pay.</h2>
  <p class="lede">We wrote a complete campaign for an illustrative small-batch skincare brand. Every email, every text, every checkout tweak. No form, no email address, no gate.</p>
  <ul class="bfcm-sendlist">
    <li><b>Email 1</b> · Early access opens tonight</li>
    <li><b>Email 2</b> · 20% off, all of it, until Monday</li>
    <li><b>Email 3</b> · For the person whose skin hates November</li>
    <li><b>Email 4</b> · Ends tomorrow — and the bundle maths</li>
    <li><b>Email 5</b> · Midnight, then it's gone</li>
    <li><b>Texts</b> · Fri 10am · Sat 5pm · Mon 9am</li>
  </ul>
  <div class="cta-row"><a class="btn btn-ghost" href="/bfcm/sample">Open the sample pack →</a></div>
</section>

<section>
  <div class="kicker"><span class="rh">// how it works</span></div>
  <h2>One required field. Forty-eight hours. No calls.</h2>
  <div class="bfcm-steps">
    <div class="bfcm-step"><span class="n">01</span><div><h3>Pay $499</h3><p>Card at checkout. You get a receipt and an order number immediately.</p></div></div>
    <div class="bfcm-step"><span class="n">02</span><div><h3>Paste your store URL</h3><p>That is the only thing you have to give us. We read your catalogue, prices, shipping threshold, and the way you already write about your products. Four optional one-liners (your offer, your platform, your top five products, voice notes) let us honour your specifics instead of inferring them. No access to your Shopify admin, no questionnaire, no kickoff call.</p></div></div>
    <div class="bfcm-step"><span class="n">03</span><div><h3>Get the drafts in 48 hours</h3><p>One document and one import-ready file: every email, every text, every checkout tweak, the calendar, and the setup checklist. Paste into Klaviyo or Shopify Email, test, schedule.</p></div></div>
  </div>
</section>

<section>
  <div class="kicker"><span class="rh">// the price</span></div>
  <h2>Why $499.</h2>
  <p class="lede">You are buying the week your store makes its year. Here is what the alternatives cost.</p>
  <table class="bfcm-table">
    <tr><th>Option</th><th>Cost</th><th>Time to drafts</th></tr>
    <tr><td>Email agency, one campaign</td><td>$2,500-5,000</td><td>2-3 weeks, with calls</td></tr>
    <tr><td>Freelance copywriter</td><td>$800-1,500</td><td>5-7 days, with a brief you write</td></tr>
    <tr><td>$49 template pack</td><td>$49 + your weekend</td><td>You still write every word</td></tr>
    <tr class="us"><td><b>Campaign-in-a-Box</b></td><td><b>$499</b></td><td><b>48 hours, one field</b></td></tr>
  </table>
  <p class="note">At a $52 average order, four extra orders covers it.</p>
</section>

<section>
  <div class="kicker"><span class="rh">// the promise</span></div>
  <h2>If it does not fit your store, you do not pay for it.</h2>
  <p class="lede">Full refund any time before we have started writing. Once the pack is delivered we do not refund it — you have the work. The one exception, and it is the one that matters: if the drafts get your product names, prices, or offer wrong, reply once within 72 hours and we refund in full. You keep the drafts either way. We would rather lose the $499 than have you send copy that embarrasses your store.</p>
</section>

<section class="bfcm-faq">
  <div class="kicker"><span class="rh">// questions</span></div>
  <h2>Before you buy.</h2>
  <details><summary>Do you send the campaign for me?</summary><p>No. You get drafts. You paste, test, and send from your own account. That is deliberate: your list, your sender reputation, your compliance. We never ask for access to your store or your email platform.</p></details>
  <details><summary>Which platform does this work with?</summary><p>Klaviyo, Shopify Email, Mailchimp, Omnisend, Brevo. The copy is plain text and HTML-ready; the merge tags are named so you can swap them in one pass.</p></details>
  <details><summary>Do I need to give you Shopify access?</summary><p>No. The only thing we need is your public store URL. We read what any visitor can read: your products, prices, shipping policy, and the copy on your product pages.</p></details>
  <details><summary>My store is not on Shopify. Does it still work?</summary><p>If your catalogue is public on the web, yes. The method does not depend on the platform.</p></details>
  <details><summary>What if I do not have an email list yet?</summary><p>Then this will not save your Black Friday, and you should not buy it. A campaign needs people to send to. Build the list first; buy this next year.</p></details>
  <details><summary>When will I get it?</summary><p>Within 48 hours of your order. Order by November 20 to have the drafts in hand before the sale week starts.</p></details>
  <details><summary>Is this AI-generated slop?</summary><p>It is written by an agent, reviewed against your live product pages, and checked for names, prices, and shipping terms before it reaches you. The sample pack is the actual standard. Read it and decide.</p></details>
</section>

<section id="checkout">
  <div class="bfcm-band">
    <div class="kicker"><span class="ra">// start</span></div>
    <h2>Five emails, three texts, three fixes. $499.</h2>
    <p>Orders close November 20 so the drafts land before Black Friday. After that the queue reopens December 1, which is too late for this year.</p>
    <div class="cta-row">
      <a class="btn btn-human" href="__CHECKOUT_URL__">Get my campaign — $499</a>
      <a class="btn btn-ghost" href="/bfcm/sample">Read the sample first →</a>
    </div>
    <p class="note">Drafts only. Delivered within 48 hours. Full refund before we start; after delivery, refund only if the drafts get your products or prices wrong.</p>
  </div>
</section>

<footer class="site"><div><a href="/">← AI Agent City</a><a href="/bfcm/sample">sample</a></div><div>Campaign-in-a-Box · BFCM 2026</div></footer>
</div>

<div class="agent-surface">
<section>
  <div class="agent-hero">
    <div class="kicker"><span class="ra">// machine-readable</span> · bfcm campaign-in-a-box</div>
    <h1>Done-for-you BFCM campaign. $499. One required field.</h1>
    <p class="lede">Human buyers only. The entry points below describe the offer; there is no agent checkout on this product.</p>
  </div>
  <div class="term"><div class="bar"><i></i><i></i><i></i><span>offer</span><span class="live">live</span></div>
  <div class="rows">
    <div class="step"><span class="t">product</span><span class="c">BFCM Campaign-in-a-Box</span><span class="r"><b>$499</b> one-time</span></div>
    <div class="step"><span class="t">includes</span><span class="c">5 emails · 3 sms · 3 checkout tweaks</span><span class="r">drafts only</span></div>
    <div class="step"><span class="t">intake</span><span class="c">store_url required · 4 optional</span><span class="r">no call</span></div>
    <div class="step"><span class="t">delivery</span><span class="c">48h from order</span><span class="r"><b>before Nov 20</b></span></div>
  </div></div>
  <div class="fine"><b>JSON</b><br>{"product":"bfcm-campaign-in-a-box","price_usd":499,"currency":"usd","includes":["5 emails","3 sms","3 abandoned-cart steps","2 browse-abandon steps","send calendar","setup checklist","Klaviyo-import-ready CSV"],"intake":{"required":["store_url"],"optional":["bfcm_offer","platform","top_products","voice_notes"]},"delivery_hours":48,"drafts_only":true,"refund":"full before start; after delivery only if product names, prices or offer are wrong (72h)"}</div>
</section>
</div>
"""

PAGE_BFCM_THANKS = """
<div class="human-surface">
<section>
  <div class="kicker"><span class="rh">// order received</span></div>
  <h1>You're in the queue.</h1>
  <p class="lede">Your campaign is being written from your store URL. Drafts land in the email you paid with <b>within 48 hours</b>, usually sooner.</p>
  <div class="bfcm-band">
    <h2>What happens next</h2>
    <p>1. We read your store: products, prices, shipping threshold, voice.<br>
       2. We write five emails, three texts, and three checkout tweaks against it.<br>
       3. You get one document and one folder. Paste, test, schedule.</p>
  </div>
  <p class="lede">If your store URL was wrong or your catalogue is password-protected, reply to the receipt email and we will sort it out before the clock matters.</p>
  <div class="cta-row"><a class="btn btn-ghost" href="/bfcm/sample">Read the sample while you wait →</a></div>
  <footer class="site"><div><a href="/">← AI Agent City</a></div><div>Campaign-in-a-Box · BFCM 2026</div></footer>
</section>
</div>
"""

# ── sample pack: rendered from the markdown, never hand-duplicated ──────────


def _md_inline(text: str) -> str:
    s = _html.escape(text)
    s = _re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", s)
    s = _re.sub(r"`(.+?)`", r"<code>\1</code>", s)
    s = _re.sub(r"\[(.+?)\]\((.+?)\)", r'<a href="\2">\1</a>', s)
    return s


def _md_to_html(md: str) -> str:
    out: list[str] = []
    para: list[str] = []
    quote: list[str] = []
    items: list[str] = []
    table: list[str] = []

    def flush() -> None:
        nonlocal para, quote, items, table
        if para:
            out.append("<p>" + _md_inline(" ".join(para)) + "</p>")
            para = []
        if quote:
            out.append("<blockquote>" + "<br>".join(_md_inline(q) for q in quote) + "</blockquote>")
            quote = []
        if items:
            out.append("<ul>" + "".join("<li>" + _md_inline(i) + "</li>" for i in items) + "</ul>")
            items = []
        if table:
            rows = [r for r in table if not _re.match(r"^\|[\s:\-|]+\|$", r)]
            body = []
            for i, row in enumerate(rows):
                cells = [c.strip() for c in row.strip().strip("|").split("|")]
                tag = "th" if i == 0 else "td"
                body.append("<tr>" + "".join("<%s>%s</%s>" % (tag, _md_inline(c), tag) for c in cells) + "</tr>")
            out.append('<table class="bfcm-table">' + "".join(body) + "</table>")
            table = []

    for line in md.splitlines():
        s = line.rstrip()
        if not s.strip():
            flush()
            continue
        if s.startswith("|"):
            if para or quote or items:
                flush()
            table.append(s)
            continue
        if table:
            flush()
        if s.startswith("> "):
            if para or items:
                flush()
            quote.append(s[2:])
            continue
        if s.startswith("- "):
            if para or quote:
                flush()
            items.append(s[2:])
            continue
        if s.startswith("### "):
            flush(); out.append("<h3>" + _md_inline(s[4:]) + "</h3>"); continue
        if s.startswith("## "):
            flush(); out.append("<h2>" + _md_inline(s[3:]) + "</h2>"); continue
        if s.startswith("# "):
            flush(); out.append("<h1>" + _md_inline(s[2:]) + "</h1>"); continue
        if s.strip() == "---":
            flush(); out.append("<hr>"); continue
        if quote:
            quote.append(s)
            continue
        if items:
            items.append(s)
            continue
        para.append(s)
    flush()
    return "\n".join(out)


def _load_sample() -> str:
    try:
        with open(SAMPLE_MD, encoding="utf-8") as fh:
            md = fh.read()
    except OSError:
        return ('<div class="human-surface"><section><h1>Sample pack</h1>'
                '<p class="lede">The sample pack is being updated. Check back shortly.</p>'
                '<div class="cta-row"><a class="btn btn-ghost" href="/bfcm">Back to the offer</a></div>'
                '</section></div>')
    return ('<div class="human-surface bfcm-sample"><section>\n'
            '<div class="kicker"><span class="rh">// sample pack</span> · illustrative store</div>\n'
            + _md_to_html(md) +
            '\n<div class="cta-row" style="margin-top:34px">'
            '<a class="btn btn-human" href="/bfcm">Get my campaign — $499</a>'
            '<a class="btn btn-ghost" href="/bfcm">Back to the offer</a></div>\n'
            '<footer class="site"><div><a href="/">← AI Agent City</a></div>'
            '<div>Campaign-in-a-Box · sample</div></footer>\n'
            '</section></div>')


PAGE_BFCM_SAMPLE = _load_sample()

if BFCM_CHECKOUT_URL:
    PAGE_BFCM = PAGE_BFCM.replace("__CHECKOUT_URL__", BFCM_CHECKOUT_URL)
else:
    PAGE_BFCM = PAGE_BFCM.replace(
        '<a class="btn btn-human" href="__CHECKOUT_URL__">Get my campaign — $499</a>',
        '<span class="btn btn-human" role="link" tabindex="-1" aria-disabled="true" '
        'style="opacity:.55;cursor:not-allowed">Checkout opens Thu · 9am ET</span>')
