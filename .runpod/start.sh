#!/usr/bin/env bash
set -Eeuo pipefail

MODE="${MODE_TO_RUN:-pod}"

case "${MODE,,}" in
  pod)
    echo "AIMODELKI ALL IN ONE: starting interactive Pod mode"
    exec /opt/workflow-launcher/scripts/start.sh
    ;;
  serverless)
    echo "AIMODELKI ALL IN ONE: starting RunPod Serverless mode"
    exec /opt/runpod-hub/.venv/bin/python -u /opt/runpod-hub/handler.py
    ;;
  *)
    echo "Invalid MODE_TO_RUN value: ${MODE}. Expected 'pod' or 'serverless'." >&2
    exit 64
    ;;
esac
