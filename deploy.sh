#!/usr/bin/env bash
# agent-ledger deterministic deploy — CLI path + post-deploy ground truth.
#
# TRIGGER REALITY (corrected 2026-09-11): GitHub auto-deploy IS wired for this
# service. The live deployment's meta carries `branch: main` and `commitHash`,
# so MERGING OR PUSHING TO MAIN DEPLOYS TO PRODUCTION AUTOMATICALLY. This
# script is therefore not the normal path — it is the manual/forced path, and
# its real value is the verification (sections 3, 6, 8): before/after hashes,
# the deployed-bytes-vs-working-tree check, and the trigger report.
#
# The historical note below is kept because it is why this file exists.
# (2026-09-10): every deploy was a manual CLI push, and one merge sat undeployed
# for an hour while /health stayed green. Hence the guardrails and hashes.
#
# NOTE: sections 6 checks only api_server.py + ledger_engine.py. For a release
# touching more than those two, use the fuller verifier instead:
#   bash ~/AI-Workbench/projects/agent-ledger/verify-deploy.sh
#   (checks all 11 shipped modules, the image's installed dependency set, and
#    the live behavioural contracts — added after the 2026-09-11 release, whose
#    two production defects were both invisible to these two hashes.)
#
# Usage: ./deploy.sh [--allow-dirty]
set -euo pipefail

export PATH="$HOME/.npm-global/bin:$PATH"
cd "$(dirname "$0")"

SERVICE_URL="https://agent-ledger-production-0ff8.up.railway.app"
KEY_FILES="api_server.py ledger_engine.py"

# ── 1. preflight ────────────────────────────────────────────────────────────
command -v railway >/dev/null || { echo "✖ railway CLI not on PATH"; exit 1; }
railway whoami >/dev/null 2>&1  || { echo "✖ not authenticated — run: railway login"; exit 1; }

PROJECT=$(railway status 2>/dev/null | awk '/^Project:/ {print $2}')
if [ "$PROJECT" != "agent-ledger" ]; then
  echo "✖ wrong/missing link (got: ${PROJECT:-none}). Fix with:"
  echo "  railway link --project 2766aa69-738a-4418-ba1c-335c74fb172e --service agent-ledger"
  exit 1
fi

# local must not be behind origin/main (deploying a stale tree silently)
git fetch origin main --quiet 2>/dev/null || true
BEHIND=$(git rev-list --count HEAD..origin/main 2>/dev/null || echo 0)
if [ "$BEHIND" != "0" ]; then
  echo "✖ local is $BEHIND commit(s) behind origin/main — pull/ff first:"; git log --oneline HEAD..origin/main | head -5; exit 1
fi

# ── 2. dirty-tree guard: railway up ships the WORKING TREE, not the commit ─
# Covers BOTH tracked modifications (git diff) and untracked files (status) —
# untracked scratch is what actually rode into the 9/10 image. .railwayignore
# filters the upload, but the guard still surfaces them so nothing surprises you.
if ! git diff --quiet 2>/dev/null || ! git diff --cached --quiet 2>/dev/null; then
  DIRTY="tracked"
elif [ -n "$(git ls-files --others --exclude-standard)" ]; then
  DIRTY="untracked"
else
  DIRTY=""
fi
if [ -n "$DIRTY" ]; then
  if [ "${1:-}" = "--allow-dirty" ]; then
    SNAP="../worktree-snapshot-$(date +%Y%m%d-%H%M%S).patch"
    git diff > "$SNAP"
    echo "⚠ $DIRTY changes — tracked diffs snapshotted to $SNAP; all of it WILL ship in the image"
  else
    echo "✖ uncommitted ($DIRTY) changes — a deploy would bake them into the image."
    echo "  Commit first, or run: ./deploy.sh --allow-dirty"
    git status -s
    exit 1
  fi
fi

# ── 3. pre-deploy fingerprint ───────────────────────────────────────────────
echo "── local hashes (pre-deploy)"
shasum -a 256 $KEY_FILES

# ── 4. deploy ───────────────────────────────────────────────────────────────
echo "── railway up --detach"
railway up --detach

# ── 5. poll until SUCCESS (max ~6 min) ──────────────────────────────────────
STATUS="BUILDING"
for i in $(seq 1 24); do
  sleep 15
  LINE=$(railway deployment list 2>/dev/null | sed -n '2p')
  STATUS=$(echo "$LINE" | grep -oE "SUCCESS|BUILDING|DEPLOYING|REMOVED|FAILED|CRASHED" | head -1)
  echo "  poll $i: ${STATUS:-unknown}"
  [ "$STATUS" = "SUCCESS" ] && break
  if [ "$STATUS" = "CRASHED" ] || [ "$STATUS" = "FAILED" ]; then
    echo "✖ deploy failed — logs: railway logs --build"; exit 1
  fi
done
[ "$STATUS" = "SUCCESS" ] || { echo "✖ timed out waiting for deploy"; exit 1; }

# ── 6. hash ground truth: deployed bytes vs working tree ───────────────────
sleep 10
REMOTE=$(railway ssh -- python3 -c "
import hashlib
for f in ('/app/api_server.py','/app/ledger_engine.py'):
    print(hashlib.sha256(open(f,'rb').read()).hexdigest()[:12], f)
" 2>/dev/null | grep -E "^[0-9a-f]{12}" || true)
LOCAL=$(shasum -a 256 $KEY_FILES | awk '{print substr($1,1,12), "/app/"$2}')
if [ "$REMOTE" = "$LOCAL" ]; then
  echo "✓ deployed files byte-identical to working tree"
else
  echo "✖ HASH MISMATCH — the live image is NOT this code:"
  echo "  remote: $REMOTE"
  echo "  local:  $LOCAL"
  exit 1
fi

# ── 7. health ───────────────────────────────────────────────────────────────
HEALTH=$(curl -s -m 10 "$SERVICE_URL/health" || echo "(no response)")
echo "✓ health: $HEALTH"

# ── 8. trigger visibility: is auto-deploy wired, or still all-CLI? ─────────
echo "── last 3 deployment triggers"
railway deployment list --json 2>/dev/null | python3 -c "
import json, sys
d = json.load(sys.stdin)
deps = d if isinstance(d, list) else d.get('deployments', [])
for x in deps[:3]:
    m = x.get('meta') or {}
    trig = m.get('cliCaller') or 'webhook/github'
    print(' ', str(x.get('createdAt','?'))[:19], x.get('status'), '| trigger:', trig)
" 2>/dev/null || true

echo "✓ deploy complete"