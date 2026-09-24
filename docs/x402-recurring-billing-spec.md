# x402 recurring billing — spec + price recommendation

**For:** principal decision (**price** + **trial coexistence**) · **Author:** Hermes
· **Date:** 2026-09-24 · **Status:** **PRICE LOCKED BY PRINCIPAL — build still HELD.**

> ## LOCKED 2026-09-24 — principal approval
>
> The principal approved the full package: *"lock in $19/30-day x402 pricing with the
> coexistence rules, and hold the recurring build until a wallet asks to renew."*
>
> **Locked, exactly one SKU:**
> - **$19.00 USDC / 30 days**, Base mainnet `eip155:8453`
> - Parity with the Stripe Starter price — **no discount, no second SKU, no annual**
> - **Renewal stacks from the EXISTING EXPIRY, never from purchase time**
> - **A pass-holder is refused WITHOUT consuming the one-time trial**
> - Trial ($0.01/24h) and the 30-day SKU **coexist** under the rules in §5
>
> **HOLD IS STILL IN FORCE: no recurring x402 code is to be built.** The build begins
> only when a wallet actually asks to renew. Nothing in this document is implemented.
>
> The analysis below is the reasoning that produced the locked numbers. It is kept
> unedited so a future reader can see what the decision rested on rather than only
> what was decided.

---

## 1. The problem in one paragraph

Today there are exactly two ways to get AgentLedger to take money, and **an agent can
only use one of them, exactly once.** `POST /v1/billing/x402` accepts real USDC on Base
mainnet — $0.01 for a 24h pass — but it is a **one-time trial per wallet, forever**. After
that, the only paid path is Stripe, which is a **payment link a human must open in a
browser**. So an agent that has used its trial and has no human has **no way to pay at
all**. The recurring SKU below is the missing rung.

## 2. What x402 can and cannot do — the constraint everything else follows from

**x402 cannot charge anyone on a schedule.** It has no mandate, no stored credential, no
off-session debit. Every payment is a request made *by* the payer. This is not a
limitation we can engineer around — it is the shape of the protocol.

**Consequence: "recurring" here means a PREPAID PERIOD that the agent re-buys.** No
auto-renewal is possible. If the agent does not come back, revenue stops — and pretending
otherwise in the spec or the copy would be a lie that shows up as churn we can't explain.

That is not a weakness worth hiding: it is the one thing that makes this rail safe.
Nothing is charged without an explicit act, so there are no surprise renewals, no
chargebacks, and no refund handling to build.

## 3. The spec (minimal — one SKU)

**`POST /v1/billing/x402` gains a `period` parameter.** Same route, same settlement,
same idempotency key.

| | |
|---|---|
| **Pass** | 30 days of the Starter tier (10 agents) |
| **Price** | $19.00 USDC |
| **Network** | Base mainnet `eip155:8453`, unchanged |
| **Identity** | paying wallet → workspace, unchanged |
| **Idempotency** | settlement tx hash, unchanged — a replayed tx must never double-extend |

**Five rules that decide whether this is trustworthy:**

1. **Renewal stacks from expiry, not from now.** An agent that renews 5 days early gets
   35 days, not 30. Billing from `now` would silently delete time the customer already
   paid for — the same class of defect as the tier downgrade found on 2026-09-24.
2. **Expiry downgrades to free (3 agents). It never deletes data.** Same posture as the
   existing Stripe grace window: losing access is acceptable, losing the ledger is not.
3. **A pass-holder hitting the trial endpoint must be refused — without burning the
   trial.** Today's gate refuses *before* settlement, which is correct. It must also
   distinguish "already subscribed" from "already used the trial", or a subscribed agent
   silently loses its one-time trial.
4. **The trial stays one per wallet, forever.** It is not a revenue line — it is the
   demand evidence the 30-day kill rule reads. Pricing it or making it repeatable
   destroys the only measurement we have.
5. **No change to the Stripe rail.** This adds a rung; it does not move the ladder.

## 4. Price recommendation — $19/mo, at parity with Stripe

**Recommendation: $19.00 for 30 days, identical to the Starter price a human pays.**

Four reasons, in order of weight:

1. **Zero arbitrage.** Same price on both rails, so no customer has a reason to pick the
   rail that is worse for us — and no one can be embarrassed by finding the price cheaper
   somewhere else on our own site.
2. **Parity is already a margin win.** Stripe takes ~2.9% + 30¢; on $19 that is $0.85.
   Crypto settlement is a fraction of that. Matching the price means the rail that is
   cheaper for us is also the one that costs the customer nothing extra.
3. **$19 is already in the Terms.** No new legal surface, no new refund policy.
4. **We have no data.** We have **zero** agent payments and **zero** third-party usage.
   Any price other than parity would be a guess dressed as strategy.

**Do NOT build a discount ladder yet** (e.g. $49/quarter). The argument for it is
settlement-fee savings — but with zero volume we would be optimising a cost that does not
exist, and every extra SKU is another surface that can disagree with the others (which is
this project's most frequent defect class). **Revisit only if the trial produces real
buyers.**

**Honest caveat, stated plainly:** $19/mo is a *human* price point, and an agent has no
salary. The right price is probably per-unit-of-work, not per-month. **We cannot know
that yet.** That is exactly what the $0.01 trial is for — so ship parity, and let the
first 30 days set the real number rather than guessing it now.

## 5. Trial coexistence — the decision that needs your call

The trial is $0.01 for 24h. The pass is $19 for 30 days. Three cases:

| Case | Recommended behaviour | Why |
|---|---|---|
| Wallet **used the trial**, then buys a pass | Allow. Pass runs 30 days from purchase. | The trial was a sample, not a discount. No proration — prorating $0.01 is noise. |
| Wallet **has an active pass**, hits trial | Refuse, **preserve** trial eligibility | Prevents burning the one-time trial on an agent that is already paid up. |
| Pass **expires**, agent re-buys | Stack from the expired date if within a grace window; else from now | Rewards a lapsed-but-returning agent without giving away unlimited back-dated time. |

**The one I would flag hardest:** case 2 is a live trap if implemented carelessly. Today's
gate refuses any wallet that has *used the trial*; a subscribed wallet has not used it, so
it would be allowed — and would burn it. Small fix, but it is the kind that goes unnoticed
until a paying customer complains.

## 6. What "done" would look like

1. `period` param + 30-day grant on the existing route, one SKU, no new endpoints
2. Rules 1–5 above, each with a test that fails when the rule is removed (canary)
3. A wallet that pays twice for the same tx hash is charged **once**
4. Copy updated on `/llms.txt` and the refusal messages to match shipped behaviour
5. Deployed commit reported against the live URL, not the push

## 7. Decisions I need from you

1. **Price** → recommend **$19.00 / 30 days**, parity with Stripe. *Alternatives: a lower
   agent-only price (cannibalises cards), or a quarterly discount (premature).*
2. **Trial coexistence** → recommend **the three rules in §5**, especially "refuse a
   pass-holder without burning the trial".
3. **Build it now, or wait for trial data?** → recommend **wait**. The kill rule says
   demand first; a recurring SKU with no buyers is the same mistake as the x402 launch you
   already killed. Build it the moment a wallet asks to renew.

**Do-nothing default:** the rail stays trial-only. Nothing breaks, nothing is at risk, and
an agent with no human still cannot pay after its trial — which is the status quo you have
today and the honest reason this spec exists.
