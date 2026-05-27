#!/bin/bash
# Sync inventory-manager source to a remote host for deployment.
# Adapt the variables below to match your environment.
#
# Usage: ./deploy/sync.sh

set -e

# ── Configure these for your environment ──────────────────────────────────
REMOTE_HOST="myserver"                          # SSH alias or user@host
APP_DEST="${REMOTE_HOST}:/path/to/apps/inventory-manager"
INVENTORY_DATA_PATH="/path/to/data/inventory"   # remote path for devices.yaml
# ──────────────────────────────────────────────────────────────────────────

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEPLOY_DIR="$(cd "$(dirname "$0")" && pwd)"
INVENTORY_SRC="$(cd "$(dirname "$0")/../../../inventory" && pwd)/devices.yaml"

# ── App source ─────────────────────────────────────────────────────────────
echo "Syncing app source → $APP_DEST"
rsync -av --delete \
  --exclude 'deploy/' \
  --exclude '__pycache__/' \
  --exclude '*.pyc' \
  "$SRC/" "$APP_DEST/"

# ── Stack/compose files ────────────────────────────────────────────────────
echo "Syncing compose files → $APP_DEST/stack/"
rsync -av \
  "$SRC/Dockerfile" \
  "$SRC/requirements.txt" \
  "$DEPLOY_DIR/compose.yaml" \
  "$APP_DEST/stack/"

# ── First-run: seed devices.yaml if not already present ───────────────────
echo "Checking devices.yaml on data volume..."
ssh "${REMOTE_HOST}" "mkdir -p ${INVENTORY_DATA_PATH}"
rsync -av --ignore-existing \
  "$INVENTORY_SRC" \
  "${REMOTE_HOST}:${INVENTORY_DATA_PATH}/devices.yaml"

echo ""
echo "Done."
echo ""
echo "First deploy — copy the stack files to your compose/Dockge stack directory,"
echo "create a .env file from deploy/.env.example, then deploy."
echo ""
echo "Code update:  re-run sync.sh → restart container"
echo "Dep update:   re-run sync.sh → rebuild image → restart"
