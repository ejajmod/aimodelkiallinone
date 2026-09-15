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

BASE_START="${RUNPOD_BASE_START:-/usr/local/bin/runpod-base-start.sh}"
LAUNCHER_PID=""
BASE_PID=""

alive() {
  { [[ -n "$LAUNCHER_PID" ]] && kill -0 "$LAUNCHER_PID" 2>/dev/null; } \
    || { [[ -n "$BASE_PID" ]] && kill -0 -- "-$BASE_PID" 2>/dev/null; }
}

shutdown() {
  trap - SIGTERM SIGINT EXIT
  [[ -n "$LAUNCHER_PID" ]] && kill -TERM "$LAUNCHER_PID" 2>/dev/null || true
  # The stock script, JupyterLab, FileBrowser and ComfyUI share one process group. The
  # stock script alone would not react while it sits in "sleep infinity" after a crash.
  [[ -n "$BASE_PID" ]] && kill -TERM -- "-$BASE_PID" 2>/dev/null || true
  # A ComfyUI started by restart-comfyui.sh runs in its own session.
  pkill -TERM -f '[p]ython.*main.py.*--port 8188' 2>/dev/null || true
  for _ in {1..20}; do
    alive || break
    sleep 0.5
  done
  if alive; then
    [[ -n "$BASE_PID" ]] && kill -KILL -- "-$BASE_PID" 2>/dev/null || true
    [[ -n "$LAUNCHER_PID" ]] && kill -KILL "$LAUNCHER_PID" 2>/dev/null || true
  fi
}
trap shutdown SIGTERM SIGINT EXIT

mkdir -p /workspace/runpod-slim

# The stock script only recreates a missing venv, so drop one left half-built by an
# interrupted first start.
COMFYUI_VENV="$COMFYUI_ROOT/.venv-cu128"
if [[ -d "$COMFYUI_VENV" && ! -x "$COMFYUI_VENV/bin/python" ]]; then
  echo "AIMODELKI: removing an incomplete ComfyUI venv from an interrupted first start"
  rm -rf -- "$COMFYUI_VENV"
fi

cd "$APP_ROOT"
python3 -m uvicorn launcher.app:app --host 0.0.0.0 --port 3000 --proxy-headers --forwarded-allow-ips='*' &
LAUNCHER_PID=$!
echo "AIMODELKI ALL IN ONE is listening on port 3000"

# JupyterLab is intentionally exposed without password or token authentication.
# RunPod or a copied template may inject JUPYTER_PASSWORD; the stock script passes it
# to Jupyter as the token, so it is cleared here.
export JUPYTER_PASSWORD=""

# Stock RunPod start: copies the baked ComfyUI into /workspace on first start, creates
# its venv and starts SSH, FileBrowser, JupyterLab and ComfyUI. setsid makes it the
# leader of its own process group, so shutdown can stop everything it started.
cd /workspace/runpod-slim
setsid "$BASE_START" &
BASE_PID=$!

wait -n "$LAUNCHER_PID" "$BASE_PID"
