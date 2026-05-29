#!/bin/bash
# update.sh — Pull the latest inventory-manager from GitHub and restart or
# rebuild the container depending on what changed.
#
# Deployed to /usr/local/bin/update-inventory-manager by sync-inventory-manager.sh,
# which substitutes the placeholder paths below with real values.
set -euo pipefail

# ── Edit these to match your TrueNAS paths ──────────────────────────────────
APP_DIR="/mnt/your-pool/apps/inventory-manager"
DATA_DIR="/mnt/your-pool/data/inventory"
STACK_DIR="/mnt/.ix-apps/app_mounts/dockge/stacks/inventory-manager"
REPO="https://github.com/Ratoka/paperless-home-inventory.git"

# ── Sync source from GitHub ──────────────────────────────────────────────────
GIT="git -c safe.directory=$APP_DIR"

if [[ -d "$APP_DIR/.git" ]]; then
    echo "Pulling latest from GitHub..."
    req_before=$(sha256sum "$APP_DIR/requirements.txt" 2>/dev/null | awk '{print $1}')
    $GIT -C "$APP_DIR" remote set-url origin "$REPO"
    $GIT -C "$APP_DIR" fetch
    $GIT -C "$APP_DIR" reset --hard origin/main
    $GIT -C "$APP_DIR" clean -fd
elif [[ -d "$APP_DIR" ]]; then
    echo "Converting app dir to git-managed (one-time migration)..."
    req_before=$(sha256sum "$APP_DIR/requirements.txt" 2>/dev/null | awk '{print $1}')
    $GIT -C "$APP_DIR" init
    $GIT -C "$APP_DIR" remote set-url origin "$REPO" 2>/dev/null || \
      $GIT -C "$APP_DIR" remote add origin "$REPO"
    $GIT -C "$APP_DIR" fetch
    $GIT -C "$APP_DIR" reset --hard origin/main
    $GIT -C "$APP_DIR" clean -fd
else
    echo "Cloning from GitHub..."
    req_before=""
    git clone "$REPO" "$APP_DIR"
fi

req_after=$(sha256sum "$APP_DIR/requirements.txt" | awk '{print $1}')

# ── Restart container ────────────────────────────────────────────────────────
cd "$STACK_DIR"
if [[ "$req_before" != "$req_after" ]]; then
    echo "WARNING: requirements.txt changed — a rebuild is needed."
    echo "Run sync-inventory-manager.sh from your local machine to rebuild."
else
    echo "Restarting container..."
    docker compose restart
fi

echo "Done."
