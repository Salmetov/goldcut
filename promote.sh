#!/usr/bin/env bash
# Promote goldcut dev -> prod, with GitHub as the source of truth.
# Flow: push dev to GitHub, fast-forward GitHub main to dev, prod pulls main, restart bot, verify active.
# Run from the dev checkout: /root/goldcut-dev/promote.sh
set -euo pipefail

DEV=/root/goldcut-dev
PROD=/root/goldcut

cd "$DEV"

[ "$(git branch --show-current)" = "dev" ] || { echo "ERROR: dev checkout is not on branch 'dev'"; exit 1; }
git diff --quiet && git diff --cached --quiet || { echo "ERROR: uncommitted changes in $DEV — commit or stash first"; exit 1; }

echo "[1/4] push dev -> GitHub"
git push origin dev

echo "[2/4] fast-forward GitHub main -> dev"
git push origin dev:main || {
  echo "ERROR: cannot fast-forward main (main has commits not in dev). Reconcile first."; exit 1; }

echo "[3/4] prod pulls main + restart bot"
git -C "$PROD" checkout main
git -C "$PROD" pull --ff-only origin main
systemctl restart goldcut-bot

echo "[4/4] verify active"
ok=0
for _ in $(seq 1 10); do
  systemctl is-active --quiet goldcut-bot && { ok=1; break; }
  curl -s -o /dev/null http://127.0.0.1 >/dev/null 2>&1 || true
done
[ "$ok" = 1 ] && echo "OK: goldcut-bot active (commit $(git -C "$PROD" rev-parse --short main))" \
             || { echo "FAIL: goldcut-bot not active. Inspect: journalctl -u goldcut-bot -n 50"; exit 1; }
