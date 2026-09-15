# Stable RunPod ComfyUI CUDA 12.8 release, pinned to the published OCI index digest.
FROM runpod/comfyui:1.4.6-cuda12.8@sha256:ce5e842ca0c7233a983ff76a83739b445172259c77a43a117453ef7e6a64d0b7

LABEL org.opencontainers.image.title="AIMODELKI ALL IN ONE" \
      org.opencontainers.image.description="ComfyUI, JupyterLab, workflow installers and Instant Models for RunPod" \
      org.opencontainers.image.version="1.5.0"

USER root
WORKDIR /opt/workflow-launcher

COPY requirements.txt ./requirements.txt
RUN python3 -m pip install --no-cache-dir -r requirements.txt

COPY launcher ./launcher
COPY catalog ./catalog
COPY scripts ./scripts
RUN chmod +x /opt/workflow-launcher/scripts/*.sh \
    && python3 scripts/bake-custom-nodes.py catalog/catalog.json /opt/comfyui-baked

ENV PYTHONUNBUFFERED=1 \
    COMFYUI_ROOT=/workspace/runpod-slim/ComfyUI \
    LAUNCHER_STATE_DIR=/workspace/.aimodelki-allinone \
    LAUNCHER_CATALOG_PATH=/opt/workflow-launcher/catalog/catalog.json \
    INSTANT_MODELS_STATE_DIR=/workspace/.instant-models \
    INSTANT_MODELS_API_URL=https://app.aimodelki.pl/api/v1/instant-models \
    INSTANT_MODELS_DOWNLOAD_HOSTS=.r2.cloudflarestorage.com \
    INSTANT_MODELS_DOWNLOAD_CONNECTIONS=64 \
    INSTANT_MODELS_DOWNLOAD_SEGMENT_MB=64 \
    INSTANT_MODELS_DISK_RESERVE_GB=5 \
    COMFYUI_BUNDLE_VERSION=1.4.6-cu128 \
    COMFYUI_BUNDLE_SHA256=15195e617e082d19e5fc07f3709d7402190cfe601afa32ab28f0aac7516108b0 \
    COMFYUI_BUNDLE_URL=https://pub-746aa51431cf4b7eac8a9cf5e44fbf58.r2.dev/runtime/aimodelki-comfyui-1.4.6-cu128.tar.gz

EXPOSE 3000 8188 8888

HEALTHCHECK --interval=20s --timeout=8s --start-period=180s --retries=6 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/health', timeout=3)" || exit 1

STOPSIGNAL SIGTERM

ENTRYPOINT ["/opt/workflow-launcher/scripts/start.sh"]
