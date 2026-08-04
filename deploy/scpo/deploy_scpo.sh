#!/bin/zsh
# Deploy Rally (SCPO Process Excellence) to prod.
# Run from any terminal while on Nike VPN, or let the Cursor agent run it directly.
# Usage: ./deploy.sh   (from inside the synced ../scpo-process-excellence folder)
#
# This is the SCPO counterpart to pase-work-tracker/deploy.sh — kept as a
# separate file (deploy/scpo/deploy_scpo.sh here, copied to deploy.sh by
# sync_scpo.sh) so it can never be confused with, or accidentally overwrite,
# the PaSE app.

set -e

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
DBX="${HOME}/.local/bin/databricks"
APP="scpo-process-excellence"
BUNDLE_APP_KEY="scpo_process_excellence"
SRC_PATH="/Workspace/Users/Spencer.Stendel@nike.com/.bundle/scpo-process-excellence/prod/files"

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
echo "Done. Rally (SCPO Process Excellence) is live with the new code."
echo "Check status: $DBX apps get $APP"
