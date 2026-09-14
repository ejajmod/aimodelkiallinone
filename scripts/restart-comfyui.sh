#!/usr/bin/env bash
set -Eeuo pipefail

COMFYUI_ROOT="${COMFYUI_ROOT:-/workspace/runpod-slim/ComfyUI}"
if [[ -z "${COMFYUI_PYTHON:-}" ]]; then
  for candidate in \
    "$COMFYUI_ROOT/.venv-cu130/bin/python" \
    "$COMFYUI_ROOT/.venv-cu128/bin/python" \
    "$COMFYUI_ROOT/.venv/bin/python"; do
    if [[ -x "$candidate" ]]; then
      COMFYUI_PYTHON="$candidate"
      break
    fi
  done
fi
COMFYUI_PYTHON="${COMFYUI_PYTHON:-$(command -v python3)}"
LOG_FILE="${COMFYUI_LOG_FILE:-/workspace/runpod-slim/comfyui-launcher.log}"
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

for _ in {1..30}; do
  if curl --silent --fail --max-time 2 http://127.0.0.1:8188/system_stats >/dev/null; then
    echo "ComfyUI restarted successfully"
    exit 0
  fi
  sleep 1
done

echo "ComfyUI did not become ready within 30 seconds; inspect $LOG_FILE" >&2
exit 1
