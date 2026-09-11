# AgentLedger — Architecture

One-paragraph-per-concept map of the system as it exists after the
workspace-identity phase (2026-09-10). Read this first in any new
session touching this repo — it replaces reconstructing the system from
git log and scattered specs.

## Layers

- `workspace_engine.py` — per-customer identity + billing state (flat
  JSON files under `DATA_DIR/workspaces/`). Owns: workspace records,
  key hashing, scarcity-window accounting, pro-status.
- `identity.py` — the one place every gate resolves "who is this caller"
  through (`resolve_agent_secret`, `resolve_workspace_key`,
  `resolve_session`, `authorize_agent_access`). No gate anywhere in the
  codebase should compare a secret/key/cookie directly — it calls into
  this module instead.
- `ledger_engine.py` — per-agent ledger core (spend entries, budgets,
  alerts, claim-on-first-write via `ensure_agent_secret`, which now
  requires a `workspace_key` on brand-new claims, resolved via
  `identity.py`).
- `routes_agents.py` / `routes_billing.py` / `routes_auth.py` —
  responsibility-scoped FastAPI routers, each independently readable
  without loading the others. Mounted onto the app in `api_server.py`.
- `api_server.py` — app creation, MCP mounting, meta endpoints (health,
  stats, llms.txt, agent.json, server.json), router includes. Nothing
  else lives here.

## The three ways a caller gets a workspace

1. **Google OAuth** (`routes_auth.py`) — a human logs in, gets a
   `workspace_key` shown once on the dashboard. Free tier or Pro
   depending on Stripe/scarcity status.
2. **x402 self-serve** (`routes_billing.py`) — an autonomous agent with
   its own wallet pays directly; the paying wallet address becomes the
   workspace identity. No human, no login, ever.
3. **Existing `agent_secret`** — an agent claimed before this phase (or
   claimed under either path above) keeps authenticating writes with its
   own secret, never needing the workspace_key again after the initial
   claim.

## Data flow, briefly

- **Claim:** `POST /v1/track` (new agent_id) → `identity.resolve_workspace_key`
  → cap check against the workspace's `agent_cap` → mint `agent_secret`,
  stamp `workspace_id.txt`.
- **Track/read:** existing `agent_secret` (writes) or `agent_secret` /
  `workspace_key` (reads, via `identity.authorize_agent_access`) authorizes.
- **Upgrade:** Stripe Checkout Session (`client_reference_id = workspace_id`)
  → webhook → `workspace_engine.mark_pro(workspace_id, ...)` — scoped to
  one workspace, never global.

## Keeping this current

Update this file in the same commit as any change to the layer list, the
identity paths, or the data flow above. A stale map is worse than none —
if a task changes one of these, it updates this file too.
