# Stable RunPod ComfyUI CUDA 13.0 release, pinned to the published OCI index digest.
FROM runpod/comfyui:1.4.6-cuda13.0@sha256:0bf75436da591e0f26d299af3741e07cb8ce8ce36566d1a7d8d78aae458e5d67

LABEL org.opencontainers.image.title="AIMODELKI ALL IN ONE" \
      org.opencontainers.image.description="ComfyUI, JupyterLab and declarative workflow installers for RunPod" \
      org.opencontainers.image.version="1.0.1"

USER root
WORKDIR /opt/workflow-launcher

COPY requirements.txt ./requirements.txt
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY launcher ./launcher
COPY catalog ./catalog
COPY scripts ./scripts
RUN chmod +x /opt/workflow-launcher/scripts/*.sh

ENV PYTHONUNBUFFERED=1 \
    COMFYUI_ROOT=/workspace/runpod-slim/ComfyUI \
    LAUNCHER_STATE_DIR=/workspace/.aimodelki-allinone \
    LAUNCHER_CATALOG_PATH=/opt/workflow-launcher/catalog/catalog.json

EXPOSE 3000 8188 8888

HEALTHCHECK --interval=20s --timeout=8s --start-period=180s --retries=6 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/health', timeout=3)" || exit 1

STOPSIGNAL SIGTERM

ENTRYPOINT ["/opt/workflow-launcher/scripts/start.sh"]
