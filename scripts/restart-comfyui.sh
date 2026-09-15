#!/usr/bin/env bash
set -Eeuo pipefail

COMFYUI_ROOT="${COMFYUI_ROOT:-/workspace/runpod-slim/ComfyUI}"
COMFYUI_URL="${COMFYUI_LOCAL_URL:-http://127.0.0.1:8188}"
READY_TIMEOUT="${COMFYUI_READY_TIMEOUT:-300}"
LOG_FILE="${COMFYUI_LOG_FILE:-/workspace/runpod-slim/comfyui-launcher.log}"

ready() {
  curl --silent --fail --max-time 2 "$COMFYUI_URL/system_stats" >/dev/null
}

wait_ready() {
  local seconds=$1
  for ((i = 0; i < seconds; i++)); do
    ready && return 0
    sleep 1
  done
  return 1
}

# Preferred path: ComfyUI-Manager restarts ComfyUI in place, so the stock RunPod start
# script keeps supervising the same process.
if ready && curl --silent --fail --max-time 5 "$COMFYUI_URL/manager/version" >/dev/null; then
  status=$(curl --silent --output /dev/null --write-out '%{http_code}' --max-time 15 \
    -X POST -H 'Content-Type: application/json' -d '{}' "$COMFYUI_URL/manager/reboot" || true)
  # The restart replaces the process mid-response, so no status (000) is expected.
  if [[ "$status" == "000" || "$status" == 2* ]]; then
    went_down=0
    for _ in {1..60}; do
      if ! ready; then went_down=1; break; fi
      sleep 1
    done
    if [[ "$went_down" == "1" ]] && wait_ready "$READY_TIMEOUT"; then
      echo "ComfyUI restarted successfully"
      exit 0
    fi
  fi
  echo "ComfyUI-Manager restart did not complete (HTTP $status); starting ComfyUI directly" >&2
fi

if [[ -z "${COMFYUI_PYTHON:-}" ]]; then
  for candidate in \
    "$COMFYUI_ROOT/.venv-cu128/bin/python" \
    "$COMFYUI_ROOT/.venv-cu130/bin/python" \
    "$COMFYUI_ROOT/.venv/bin/python"; do
    if [[ -x "$candidate" ]]; then
      COMFYUI_PYTHON="$candidate"
      break
    fi
  done
fi
COMFYUI_PYTHON="${COMFYUI_PYTHON:-$(command -v python3)}"
ARGS=(--listen 0.0.0.0 --port 8188 --enable-cors-header)

if [[ ! -x "$COMFYUI_PYTHON" || ! -f "$COMFYUI_ROOT/main.py" ]]; then
  echo "ComfyUI is not initialized yet at $COMFYUI_ROOT" >&2
  exit 1
fi

pkill -f '[p]ython.*main.py.*--port 8188' 2>/dev/null || true
sleep 2

if [[ -s /workspace/runpod-slim/comfyui_args.txt ]]; then
  while IFS= read -r argument; do
    [[ -z "$argument" || "$argument" == \#* ]] && continue
    read -r -a PARTS <<< "$argument"
    ARGS+=("${PARTS[@]}")
  done < /workspace/runpod-slim/comfyui_args.txt
fi

cd "$COMFYUI_ROOT"
nohup "$COMFYUI_PYTHON" main.py "${ARGS[@]}" >> "$LOG_FILE" 2>&1 &
echo $! > /workspace/runpod-slim/comfyui-launcher.pid

if wait_ready "$READY_TIMEOUT"; then
  echo "ComfyUI restarted successfully"
  exit 0
fi

echo "ComfyUI did not become ready within ${READY_TIMEOUT} seconds; inspect $LOG_FILE" >&2
exit 1
