# AgentLedger — Architecture

One-paragraph-per-concept map of the system as it exists after the
workspace-identity phase (2026-09-10). Read this first in any new
session touching this repo — it replaces reconstructing the system from
git log and scattered specs.

## Layers

- `workspace_engine.py` — per-customer identity + billing state (flat
  JSON files under `DATA_DIR/workspaces/`). Owns: workspace records,
  key hashing, scarcity-window accounting, pro-status.
  `create_workspace()` is a pure lookup-or-mint: for an identity that
  already has a workspace it returns `(workspace_id, None)` and changes
  nothing. Minting a replacement key is the separate, explicit
  `reissue_key()`, deliberately wired into no automatic path (key
  rotation is a future phase) — an identity lookup must never invalidate
  a key as a side effect.
- `identity.py` — the one place every gate resolves "who is this caller"
  through (`resolve_agent_secret`, `resolve_workspace_key`,
  `authorize_agent_access`). No gate anywhere in the codebase should
  compare a secret/key directly — it calls into this module instead.
  `authorize_agent_access` accepts either of two credentials, both
  header-borne (agent_secret, workspace_key); the latter is checked
  against the agent's `workspace_id.txt`, so it cannot reach another
  workspace's agents. The browser-session arm was removed with Google
  sign-in (D-1162): with no login there is no cookie that could
  authorize a read.
- `al_mcp_http.py` — the hosted MCP surface. Credential-equivalent to
  REST: `ledger_track`/`ledger_set_budget` take `workspace_key` to claim
  and `agent_secret` thereafter, and `ledger_report`/`ledger_alerts`
  require one of the two read credentials through the same
  `identity.authorize_agent_access`. It cannot raise HTTP status codes,
  so errors return `{"error", "error_code"}` using the REST code names.
- `ledger_engine.py` — per-agent ledger core (spend entries, budgets,
  alerts, claim-on-first-write via `ensure_agent_secret`, which now
  requires a `workspace_key` on brand-new claims, resolved via
  `identity.py`).
- `routes_agents.py` / `routes_billing.py` — responsibility-scoped
  FastAPI routers, each independently readable without loading the others.
  Mounted onto the app in `api_server.py`. There is no third auth router:
  Google OAuth was deleted in D-1162, and the human path lives in
  `api_server.py` as `GET`/`POST /start`.
- `api_server.py` — app creation, MCP mounting, meta endpoints (health,
  stats, llms.txt, agent.json, server.json), router includes. Nothing
  else lives here.

## The three ways a caller gets a workspace

1. **`POST /start`** (`api_server.py`) — a human, with no signup, no login
   and no card. The workspace is minted and its `workspace_key` is shown
   once, on the spot. Nothing is emailed: the key is only ever stored as a
   hash and there is no recovery flow in this version. The same page hands
   over the payment link for THAT workspace
   (`?client_reference_id=workspace_id`), which is what makes the Stripe
   webhook upgrade the right workspace. **`GET /start` deliberately does
   not mint** — a crawler or link-preview bot hitting the page must not
   burn a launch-window workspace and orphan a key nobody holds.
2. **x402 self-serve** (`routes_billing.py`) — an autonomous agent with
   its own wallet pays directly; the paying wallet address becomes the
   workspace identity. No human, no login, ever. The settlement
   `tx_hash` is the idempotency key, on the same store `/v1/track` uses:
   a replay returns the identical cached original response, and a second
   real payment from a known wallet resolves to the existing workspace
   with `workspace_key: null` rather than rotating the key. Verification and
   settlement run through the real `x402` SDK (`x402_verify.py`); the route
   needs `X402_PAY_TO` (receiving wallet — no default) and optionally
   `X402_FACILITATOR_URL` (default `https://x402.org/facilitator`),
   `X402_NETWORK` (default `eip155:84532`, Base Sepolia) and `X402_MINT_PRICE`
   (default `$0.01`). Unset `X402_PAY_TO` degrades the route to 503; the
   service still starts.
3. **Existing `agent_secret`** — an agent claimed before this phase (or
   claimed under either path above) keeps authenticating writes with its
   own secret, never needing the workspace_key again after the initial
   claim.

## Data flow, briefly

- **Claim:** `POST /v1/track` (new agent_id) → `identity.resolve_workspace_key`
  → cap check against `workspace_engine.effective_agent_cap()` → mint
  `agent_secret`, stamp `workspace_id.txt`. The cap is read through that
  helper, not off the raw `agent_cap` field, because a scarcity workspace
  stores `agent_cap: None` permanently — the helper applies the grant's
  one-year `pro_until` expiry and falls back to the free-tier cap.
- **Track/read:** existing `agent_secret` (writes) or `agent_secret` /
  `workspace_key` (reads, via `identity.authorize_agent_access`)
  authorizes. Applies equally to the REST endpoints and the MCP read
  tools. Both credentials are headers; there is no cookie path.
- **Upgrade:** Stripe Checkout Session (`client_reference_id = workspace_id`)
  → webhook → `workspace_engine.mark_pro(workspace_id, ...)` — scoped to
  one workspace, never global.

## Keeping this current

Update this file in the same commit as any change to the layer list, the
identity paths, or the data flow above. A stale map is worse than none —
if a task changes one of these, it updates this file too.
