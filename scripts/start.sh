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
export COMFYUI_BUNDLE_VERSION="${COMFYUI_BUNDLE_VERSION:-1.4.6-cu128}"
export COMFYUI_BUNDLE_SHA256="${COMFYUI_BUNDLE_SHA256:-15195e617e082d19e5fc07f3709d7402190cfe601afa32ab28f0aac7516108b0}"
export COMFYUI_BUNDLE_URL="${COMFYUI_BUNDLE_URL:-https://pub-746aa51431cf4b7eac8a9cf5e44fbf58.r2.dev/runtime/aimodelki-comfyui-1.4.6-cu128.tar.gz}"

cd "$APP_ROOT"
python3 -m uvicorn launcher.app:app --host 0.0.0.0 --port 3000 --proxy-headers --forwarded-allow-ips='*' &
LAUNCHER_PID=$!

shutdown() {
  kill "${LAUNCHER_PID:-}" "${JUPYTER_PID:-}" "${COMFYUI_PID:-}" 2>/dev/null || true
  # restart-comfyui.sh replaces the initial process, so also stop its successor.
  pkill -f '[p]ython.*main.py.*--port 8188' 2>/dev/null || true
}
trap shutdown EXIT SIGTERM SIGINT

mkdir -p /workspace/runpod-slim
echo "AIMODELKI ALL IN ONE is listening on port 3000"

jupyter lab --ip=0.0.0.0 --port=8888 --no-browser --allow-root \
  --ServerApp.token='' --ServerApp.password='' --ServerApp.allow_origin='*' \
  --ServerApp.root_dir=/workspace > /workspace/runpod-slim/jupyter.log 2>&1 &
JUPYTER_PID=$!

if python3 "$APP_ROOT/scripts/bootstrap-comfyui.py"; then
  cd "$COMFYUI_ROOT"
  python3 main.py --listen 0.0.0.0 --port 8188 --enable-cors-header \
    > /workspace/runpod-slim/comfyui.log 2>&1 &
  COMFYUI_PID=$!
else
  echo "AIMODELKI: launcher and Jupyter remain available; ComfyUI bootstrap will retry after a container restart" >&2
fi

# ComfyUI is restarted by the launcher after installs, so it must not end the container.
wait -n "$LAUNCHER_PID" "$JUPYTER_PID"
