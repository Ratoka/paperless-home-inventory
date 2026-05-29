#!/bin/bash
# update-inventory-manager — Run on TrueNAS as root after sync-inventory-manager.sh
# has pushed the latest files. Copies stack files to Dockge and restarts or
# rebuilds the container depending on whether requirements changed.
set -euo pipefail

APP_DIR="/mnt/zfs-acd-01/apps/inventory-manager"
STACK_DIR="/mnt/.ix-apps/app_mounts/dockge/stacks/inventory-manager"

req_before=$(sha256sum "$STACK_DIR/requirements.txt" 2>/dev/null | awk '{print $1}')

echo "Copying stack files to Dockge..."
cp "$APP_DIR/stack/Dockerfile"        "$STACK_DIR/"
cp "$APP_DIR/stack/requirements.txt"  "$STACK_DIR/"
cp "$APP_DIR/stack/compose.yaml"      "$STACK_DIR/"

req_after=$(sha256sum "$STACK_DIR/requirements.txt" | awk '{print $1}')

cd "$STACK_DIR"
if [[ "$req_before" != "$req_after" ]]; then
    echo "requirements.txt changed — rebuilding image..."
    docker compose up --build -d
else
    echo "Restarting container..."
    docker compose restart
fi

echo "Done."
