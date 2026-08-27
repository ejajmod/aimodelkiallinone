#!/usr/bin/env bash
set -Eeuo pipefail

BAKED_APP_ROOT="/opt/workflow-launcher"
APP_ROOT="$BAKED_APP_ROOT"

if [[ "${LAUNCHER_UPDATE_ENABLED:-0}" == "1" && -n "${LAUNCHER_UPDATE_REPO:-}" ]]; then
  UPDATE_ROOT="/tmp/aimodelki-allinone-update"
  rm -rf "$UPDATE_ROOT"
  if git clone --depth 1 --branch "${LAUNCHER_UPDATE_REF:-main}" "$LAUNCHER_UPDATE_REPO" "$UPDATE_ROOT" \
    && [[ -f "$UPDATE_ROOT/launcher/app.py" && -f "$UPDATE_ROOT/requirements.txt" ]]; then
    python3 -m pip install --no-cache-dir -r "$UPDATE_ROOT/requirements.txt"
    APP_ROOT="$UPDATE_ROOT"
    echo "AIMODELKI ALL IN ONE: using optional launcher update from ${LAUNCHER_UPDATE_REPO}@${LAUNCHER_UPDATE_REF:-main}"
  else
    echo "AIMODELKI ALL IN ONE: optional update failed validation; using the baked launcher" >&2
  fi
fi

export LAUNCHER_APP_ROOT="$APP_ROOT"
export LAUNCHER_CATALOG_PATH="${LAUNCHER_CATALOG_PATH:-$APP_ROOT/catalog/catalog.json}"
export LAUNCHER_STATE_DIR="${LAUNCHER_STATE_DIR:-/workspace/.aimodelki-allinone}"
export COMFYUI_ROOT="${COMFYUI_ROOT:-/workspace/runpod-slim/ComfyUI}"

cd "$APP_ROOT"
python3 -m uvicorn launcher.app:app --host 0.0.0.0 --port 3000 --proxy-headers --forwarded-allow-ips='*' &
LAUNCHER_PID=$!

shutdown() {
  kill "$LAUNCHER_PID" 2>/dev/null || true
}
trap shutdown EXIT SIGTERM SIGINT

echo "AIMODELKI ALL IN ONE is listening on port 3000"
mkdir -p /workspace/runpod-slim
cd /workspace/runpod-slim

# JupyterLab is intentionally exposed without password or token authentication.
# RunPod or a copied template may inject JUPYTER_PASSWORD; override it so the
# base image always passes an empty IdentityProvider token to Jupyter Server.
export JUPYTER_PASSWORD=""
exec /start.sh
