#!/usr/bin/env bash
# Syncs the shared Rally codebase into the sibling SCPO deploy folder.
#
# Why this exists: Databricks Asset Bundles refuse to let two `apps`
# resources share the same `source_code_path` inside one bundle, and a
# bundle's `source_code_path` can't reach outside its own directory either.
# So PaSE and SCPO each need their own physical copy of the code to deploy
# from — this script is what keeps SCPO's copy identical to this repo.
# Both orgs still share 100% of the Python source; the only per-org
# differences live in orgs/registry.py (picked at runtime via RALLY_ORG),
# never in the file layout.
#
# Usage:
#   ./sync_scpo.sh [DEST_DIR]
#
#   DEST_DIR defaults to ../scpo-process-excellence (sibling of this repo).
#
# After syncing, deploy from DEST_DIR like any other bundle:
#   cd ../scpo-process-excellence
#   databricks bundle deploy --target prod --profile DEFAULT
#   databricks bundle run scpo_process_excellence --target prod --profile DEFAULT
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST_DIR="${1:-$SRC_DIR/../scpo-process-excellence}"

mkdir -p "$DEST_DIR"
DEST_DIR="$(cd "$DEST_DIR" && pwd)"

echo "Syncing $SRC_DIR -> $DEST_DIR"

rsync -a --delete \
  --exclude '.git/' \
  --exclude '.venv/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  --exclude '.pytest_cache/' \
  --exclude '.databricks/' \
  --exclude '.env' \
  --exclude 'data/pase_work_tracker/' \
  --exclude 'databricks.yml' \
  --exclude 'deploy/' \
  --exclude 'sync_scpo.sh' \
  --exclude 'deploy.sh' \
  --exclude 'app.yaml' \
  --exclude '.vscode/' \
  "$SRC_DIR/" "$DEST_DIR/"

cp "$SRC_DIR/deploy/scpo/databricks.yml" "$DEST_DIR/databricks.yml"
cp "$SRC_DIR/deploy/scpo/deploy_scpo.sh" "$DEST_DIR/deploy.sh"
cp "$SRC_DIR/deploy/scpo/app.yaml" "$DEST_DIR/app.yaml"
chmod +x "$DEST_DIR/deploy.sh"

echo "Done. SCPO deploy copy is ready at: $DEST_DIR"
echo "Next: cd '$DEST_DIR' && databricks bundle deploy --target prod --profile DEFAULT"
