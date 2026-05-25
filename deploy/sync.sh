#!/bin/bash
# Sync inventory-manager source to TrueNAS for Dockge deployment.
# Run from anywhere: ./paperless/scripts/inventory_manager/deploy/sync.sh

set -e

APP_DEST="truenas:/mnt/zfs-acd-01/apps/inventory-manager"
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

# ── Dockge stack files alongside the app source ────────────────────────────
# Requires root to write directly to the Dockge stack directory.
# On first deploy, SSH in and copy these to the Dockge stack dir manually.
echo "Syncing Dockge stack files → $APP_DEST/stack/"
rsync -av \
  "$SRC/Dockerfile" \
  "$SRC/requirements.txt" \
  "$DEPLOY_DIR/compose.yaml" \
  "$APP_DEST/stack/"

# ── First-run: seed devices.yaml if not already on the data volume ─────────
echo "Checking devices.yaml on data volume..."
ssh truenas "mkdir -p /mnt/zfs-acd-01/media/docs/inventory"
rsync -av --ignore-existing \
  "$INVENTORY_SRC" \
  "truenas:/mnt/zfs-acd-01/media/docs/inventory/devices.yaml"

echo ""
echo "Done."
echo ""
echo "First deploy — run once as root on TrueNAS:"
echo "  STACK=/mnt/.ix-apps/app_mounts/dockge/stacks/inventory-manager"
echo "  mkdir -p \$STACK"
echo "  cp /mnt/zfs-acd-01/apps/inventory-manager/stack/* \$STACK/"
echo "  Then open Dockge → inventory-manager → set PAPERLESS_TOKEN in env → Deploy"
echo ""
echo "Code update:   re-run sync.sh → Dockge Restart"
echo "Dep update:    re-run sync.sh → Dockge Rebuild → Restart"
