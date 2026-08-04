#!/bin/zsh
# Deploy Rally to prod.
# Run from any terminal while on Nike VPN, or let the Cursor agent run it directly.
# Usage: ./deploy.sh

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DBX="${HOME}/.local/bin/databricks"
APP="pase-work-tracker"
SRC_PATH="/Workspace/Users/Spencer.Stendel@nike.com/.bundle/${APP}/prod/files"

cd "$SCRIPT_DIR"

echo "==> Clearing local sync cache (forces full upload)..."
rm -rf .databricks/bundle/prod/sync-snapshots

echo "==> Deploying bundle to prod..."
"$DBX" bundle deploy --target prod --profile DEFAULT

echo "==> Pushing app source..."
"$DBX" apps deploy "$APP" --source-code-path "$SRC_PATH" --profile DEFAULT

echo "==> Restarting app to load new code..."
"$DBX" apps stop "$APP" --profile DEFAULT >/dev/null 2>&1 || true
sleep 3
"$DBX" apps start "$APP" --profile DEFAULT >/dev/null 2>&1 || true

echo ""
echo "Done. Rally is live with the new code."
echo "Check status: $DBX apps get $APP"
