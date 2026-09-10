# AgentLedger — Workspace Identity & Per-Customer Billing (Phase 1 of 4)

**Status:** Design approved by user 2026-09-10. Not yet planned or implemented.
**Phase:** 1 of 4 (identity/billing core → agent-facing API change → dashboard UI → migration cutover)

## Why this exists

AgentLedger shipped as a single shared instance. Verified in code during a
customer-zero walkthrough (2026-09-10): the 3-agent free cap is a global
count across *every* user of the instance combined, and the Stripe webhook
flips one file (`pro.flag`) that removes the cap for *everyone*, not just
the payer. There is no per-customer isolation. This is fine for a solo
dogfood/beta phase; it does not support "50 real customers, each running
their own agents" — the free tier alone would be exhausted by a handful of
real users, and one person's $19 currently uninstalls the paywall for the
whole world.

The user (technical founder, non-coder) wants the standard SaaS-agent
pattern used by OpenRouter/Router.com/Command Code: customer signs up
(Google OAuth), gets an identity + key, subscribes via Stripe, and only
their own agents get the benefit of their subscription.

## Scope of this phase

This spec covers **only** the identity/billing core: the workspace data
model, Google OAuth + sessions, the claim-flow change, and per-workspace
Stripe billing. It explicitly does NOT cover the dashboard UI pages
(Phase 3) or the full migration script implementation (Phase 4) — those get
their own specs once this shape is locked, per the brainstorming skill's
decomposition guidance for multi-subsystem requests.

## Design

### 1. Data model — new `workspace` entity

```
workspace:
  workspace_id: str (generated, e.g. "ws_" + token_urlsafe)
  owner_email: str
  google_sub: str          # Google's stable subject id, not email (email can change)
  workspace_key_hash: str  # sha256 of the key; the raw key is shown once, like agent_secret today
  stripe_customer_id: str | None
  plan: "free" | "pro"
  agent_cap: int           # 3 for free, unbounded for pro
  created_at: float
  pro_scarcity: bool        # true if this workspace claimed one of the first-50 slots
  pro_until: float | None   # scarcity grant expiry, mirrors today's per-agent field
```

Existing `agent_id` records (unchanged file-per-agent layout) gain one new
field: `workspace_id`. This is the only change to the existing per-agent
storage — `agent_secret`, ledgers, alerts, budgets all stay exactly as they
are today.

### 2. Claim flow — the real behavioral change

`ensure_agent_secret()` (today: claim-on-first-write, no identity check
beyond the beta/scarcity cap logic from D-818/D-819) gains a precondition
for **new** claims only:

- `POST /v1/track` or `/v1/budget` for an `agent_id` that has never been
  claimed now requires a `workspace_key` field in the body.
- The key is hashed and looked up against a `workspace`. Invalid/missing
  key on a new claim → 401 `workspace_key_required` (not the old
  `beta_cap_exceeded` — that error still exists for a *valid* workspace
  that's out of free slots).
- Once claimed, all future writes to that `agent_id` continue to
  authenticate with `agent_secret` exactly as today — `workspace_key` is
  never needed again for that agent. This is deliberate: it keeps the
  blast radius on the write path at zero. `cc-tracked.py` and every other
  already-integrated caller keeps working unchanged after migration.

This removes the anonymous "curl it in 30 seconds, no signup" flow that
tested well with the fresh external agent. That tradeoff was made
explicitly by the user (Key-first / OpenRouter model) in favor of correct
per-customer billing. Documented here so it isn't rediscovered as a
surprise regression later.

### 3. Getting a workspace_key — Google OAuth + session dashboard

- `GET /login` → redirects to Google OAuth consent.
- `GET /auth/google/callback` → exchanges code, resolves `google_sub` +
  email, creates (or finds) the workspace, sets a signed session cookie.
- `GET /dashboard` (session-protected) → shows the workspace's
  `workspace_key` (shown in full — it's not a secret shown once like
  `agent_secret`, since it needs to be re-copyable from a logged-in
  session), the workspace's agents (reusing the existing per-agent
  `/v1/report/{agent_id}/html` view), and an upgrade button.
- `GET /logout` → clears session.
- Requires a Google OAuth app registered in Google Cloud Console — a
  one-time manual setup step for the user (client_id/secret become new
  Railway env vars: `GOOGLE_OAUTH_CLIENT_ID`, `GOOGLE_OAUTH_CLIENT_SECRET`).
  Not something that can be automated from this side.

### 3b. Getting a workspace_key — Path B: x402 self-serve (no human, no login)

Google OAuth (3) assumes a human owner. x402 exists specifically so an
autonomous agent with its own wallet never needs one — requiring login
for Path A would make AgentLedger unusable for that entire class of
customer, so Path B is a first-class second path, not a fallback:

- `POST /v1/billing/x402` — the agent presents a real x402 payment
  following the actual protocol (signed stablecoin transfer authorization,
  typically USDC on Base), verified against a settlement facilitator
  (Coinbase's reference facilitator — the one with real production
  volume, not a custom one).
- On confirmed settlement, the **paying wallet address becomes the
  workspace identity** — no email, no OAuth, nothing human. This is what
  makes isolation trivial here: two different wallets are, by
  construction, two different workspaces. There is no shared-identity path
  for this route to collide on.
- The settlement transaction hash is the idempotency key: a replayed or
  resubmitted payment can mint a workspace exactly once, never twice
  (checked before workspace creation, not after — a payment that already
  minted a workspace returns that workspace's existing key, not a fresh
  one).
- The minted workspace behaves identically to a Path A workspace from
  that point on (same `workspace` record shape, same claim flow, same
  agent_cap rules) — the two paths differ only in how identity is
  established, not in what a workspace is.

### 3c. Reads now require proof of workspace ownership (breaking change vs. today)

Today, `/v1/report/{agent_id}`, `/v1/tokens/{agent_id}`,
`/v1/alerts/{agent_id}`, and `/v1/report/{agent_id}/html` are open reads —
no credential required, by design, so an owner could share a link freely.
At real multi-tenant scale this means any two people who know or guess
each other's `agent_id` can read each other's spend data, which stops
being acceptable once real paying customers exist.

New behavior: each read endpoint requires **either** the agent's own
`agent_secret` **or** its workspace's `workspace_key`, passed as a header
(`X-Agent-Secret` or `X-Workspace-Key` — not a query param, to keep
credentials out of server/proxy access logs). Either credential is
sufficient (mirrors the existing write-path trust model, where
`agent_secret` alone already authorizes writes). No credential → 401.

This is a real regression against today's "share a link, anyone can see
it" pitch, made deliberately in exchange for actual data isolation
between customers — noted here so it's a decision on record, not a
surprise when someone's shared link stops working.

### 3d. Storage stays on flat files for this phase — database migration deferred

Explicit decision: workspace records use the same JSON-file-per-record
pattern as today's agent storage, not a new database. This comfortably
supports hundreds to low-thousands of agents/workspaces — a realistic
ceiling for the next stage of growth, not a permanent one. A move to a
real database (e.g. Postgres, indexed by `workspace_id`, enabling
horizontal scaling toward "millions of agents") is real, necessary work
for that future scale, but is explicitly **not** part of this phase — it
gets triggered and spec'd separately when real usage approaches the flat-
file ceiling, not built speculatively now against usage that doesn't
exist yet.

### 4. Billing — per-workspace, not global

Today: one static `buy.stripe.com/...` link, webhook flips a single global
`pro.flag`. New:

- `POST /v1/billing/checkout` (session-protected) creates a Stripe Checkout
  Session server-side with `client_reference_id = workspace_id`, returns
  the redirect URL.
- The existing `/stripe/webhook` handler (HMAC-verified, fail-closed —
  unchanged) reads `client_reference_id` from the completed session and
  sets `plan = "pro"` on *that* workspace only, via `stripe_customer_id`
  stored for future reference (e.g. cancellation webhooks, out of scope
  for this phase).
- `pro_active()` becomes a per-workspace lookup instead of a single global
  boolean file. The `AL_PRO_ACTIVE=1` operator override env var stays, for
  emergency manual unlocks.

### 5. Scarcity window — re-scoped to workspaces

"First 50 claims get Pro free for a year" becomes **first 50 *workspaces*
ever created** (checked at workspace-creation time, i.e. first login), not
first 50 raw `agent_id`s. This also closes a real gap in the current
design: today, a single person could claim 50 distinct `agent_id`s solo and
exhaust the entire scarcity pool before any other real customer got a
slot. Scoping to workspaces means the mechanic actually reaches 50 distinct
customers.

### 6. Migration (design only — implementation is Phase 4)

A one-time script creates a single default workspace (owner: the operator's
own email) and assigns the 4 currently-real `agent_id`s
(`vega-trading-desk`, `verify-agent-1`, `hermes-fleet-dogfood`,
`abhishek-command-code`) to it, preserving their existing `agent_secret`s
untouched. No behavior changes for those agents; they simply gain a
`workspace_id` field.

## Explicitly out of scope for this phase

- Dashboard UI page design/styling (Phase 3, own spec)
- Migration script implementation details, dry-run tooling (Phase 4, own spec)
- Subscription cancellation/downgrade handling beyond storing
  `stripe_customer_id` (noted as a gap, not solved here)
- Multiple team members per workspace (single `owner_email` per workspace
  for now — YAGNI until a real customer asks)
- Rate limiting per workspace — a real gap at scale (one noisy workspace
  could degrade service for others), not designed here, flagged for a
  future phase
- Database migration (flat files → real DB) — see 3d; future phase,
  triggered by real usage approaching the flat-file ceiling
- workspace_key / agent_secret rotation or revocation (e.g. after a leak)
  — no endpoint exists for this today or in this phase; real gap, noted
  for a future phase
- x402 double-spend/replay edge cases beyond the single idempotency check
  in 3b (e.g. facilitator downtime, partial settlement) — follow whatever
  the reference facilitator's own guidance is at implementation time,
  not re-invented here

## Open risks flagged, not resolved here

- Removing anonymous claim-on-first-write (Path A only — Path B stays
  frictionless for its audience) is a real product-strategy tradeoff, not
  just an implementation detail — it trades the tested-well-with-a-fresh-
  agent frictionless discovery flow for correct billing isolation, for
  the human-operator segment specifically. Worth a Strat-gate sanity check
  before this ships, per the Workbench's own monetization-decision
  routing rule.
- Scoping reads to workspace ownership (3c) is a breaking change to
  today's "share a link, anyone can see it" behavior — worth confirming
  no existing integration (dashboards, shared links already sent to
  anyone) depends on the current open-read behavior before this ships.
