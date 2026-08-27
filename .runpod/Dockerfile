FROM aimodelki/aimodelki-allin1:1.0.1@sha256:015cfcd54c16250466c335c300a1dcdee79bb49659a9b195b52d9041349d2846

LABEL org.opencontainers.image.title="AIMODELKI ALL IN ONE" \
      org.opencontainers.image.description="Dual-mode RunPod Hub image for the AIMODELKI ComfyUI launcher" \
      org.opencontainers.image.version="1.1.0"

USER root
WORKDIR /opt/runpod-hub

COPY .runpod/requirements.txt ./requirements.txt
RUN python3 -m venv --system-site-packages /opt/runpod-hub/.venv \
    && /opt/runpod-hub/.venv/bin/python -m pip install --no-cache-dir -r requirements.txt

COPY handler.py ./handler.py
COPY .runpod/start.sh ./start.sh
RUN chmod +x /opt/runpod-hub/start.sh

ENV PYTHONUNBUFFERED=1 \
    MODE_TO_RUN=pod \
    AIMODELKI_VERSION=1.1.0 \
    LAUNCHER_CATALOG_PATH=/opt/workflow-launcher/catalog/catalog.json

HEALTHCHECK --interval=20s --timeout=8s --start-period=180s --retries=6 \
  CMD if [ "${MODE_TO_RUN:-pod}" = "serverless" ]; then pgrep -f "/opt/runpod-hub/handler.py" >/dev/null; else python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/health', timeout=3)"; fi

STOPSIGNAL SIGTERM
ENTRYPOINT ["/opt/runpod-hub/start.sh"]
