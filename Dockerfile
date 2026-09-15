# syntax=docker/dockerfile:1.7
#
# Default runtime: RunPod ComfyUI on CUDA 13.0 (NVIDIA driver >= 580; Turing and newer,
# including RTX 5090 and RTX 4090). Build the compatibility variant for drivers 570-579
# and Volta GPUs with:
#   --build-arg BASE_IMAGE=runpod/comfyui:1.4.6-cuda12.8@sha256:ce5e842ca0c7233a983ff76a83739b445172259c77a43a117453ef7e6a64d0b7
#   --build-arg CUDA_VARIANT=cu128
ARG BASE_IMAGE=runpod/comfyui:1.4.6-cuda13.0@sha256:0bf75436da591e0f26d299af3741e07cb8ce8ce36566d1a7d8d78aae458e5d67
FROM ${BASE_IMAGE}

ARG CUDA_VARIANT=cu130

LABEL org.opencontainers.image.title="AIMODELKI ALL IN ONE" \
      org.opencontainers.image.description="ComfyUI, JupyterLab, workflow installers and Instant Models for RunPod" \
      org.opencontainers.image.version="1.6.0"

USER root

# aria2 downloads the models. The stock RunPod start script is kept and run next to
# the launcher, so ComfyUI, its venv, SSH, FileBrowser and JupyterLab come from the base.
RUN apt-get update \
    && apt-get install -y --no-install-recommends aria2 \
    && rm -rf /var/lib/apt/lists/* \
    && mv /start.sh /usr/local/bin/runpod-base-start.sh

WORKDIR /opt/workflow-launcher

COPY requirements.txt ./requirements.txt
RUN python3 -m pip install --no-cache-dir -r requirements.txt

# Python dependencies of every catalog custom node, installed ahead of time so that the
# per-package node installation on the Pod only confirms them. The base image keeps its
# torch, numpy, pillow, transformers and OpenCV builds; the build fails if torch moves.
COPY docker/custom-node-requirements.txt /tmp/custom-node-requirements.txt
RUN set -eu; \
    python3 -m pip freeze \
      | grep -iE '^(torch|torchvision|torchaudio|numpy|pillow|transformers|opencv-python|opencv-python-headless)==' \
      > /opt/aimodelki-node-constraints.txt; \
    sed -n 's/^opencv-python==/opencv-contrib-python==/p' /opt/aimodelki-node-constraints.txt \
      >> /opt/aimodelki-node-constraints.txt; \
    expected_torch="$(sed -n 's/^torch==//p' /opt/aimodelki-node-constraints.txt)"; \
    test -n "$expected_torch"; \
    python3 -m pip install --no-cache-dir \
      -c /opt/aimodelki-node-constraints.txt \
      -r /tmp/custom-node-requirements.txt; \
    actual_torch="$(python3 -c 'import torch; print(torch.__version__)')"; \
    if [ "$actual_torch" != "$expected_torch" ]; then \
      echo "torch changed during the custom-node install: $actual_torch != $expected_torch"; \
      exit 1; \
    fi; \
    rm /tmp/custom-node-requirements.txt

ENV PYTHONUNBUFFERED=1 \
    AIMODELKI_CUDA_VARIANT=${CUDA_VARIANT} \
    COMFYUI_ROOT=/workspace/runpod-slim/ComfyUI \
    LAUNCHER_STATE_DIR=/workspace/.aimodelki-allinone \
    LAUNCHER_CATALOG_PATH=/opt/workflow-launcher/catalog/catalog.json \
    INSTANT_MODELS_STATE_DIR=/workspace/.instant-models \
    INSTANT_MODELS_API_URL=https://app.aimodelki.pl/api/v1/instant-models \
    INSTANT_MODELS_DOWNLOAD_HOSTS=.r2.cloudflarestorage.com \
    INSTANT_MODELS_DOWNLOAD_CONNECTIONS=64 \
    INSTANT_MODELS_DOWNLOAD_SEGMENT_MB=64 \
    INSTANT_MODELS_DISK_RESERVE_GB=5 \
    AIMODELKI_DOWNLOAD_PARALLEL_FILES=4 \
    AIMODELKI_DOWNLOAD_CONNECTIONS_PER_FILE=16

EXPOSE 3000 8188 8888

# Launcher code changes most often, so it is copied last and stays a small layer.
COPY docker ./docker
COPY catalog ./catalog
COPY --chmod=0755 scripts ./scripts
COPY launcher ./launcher

HEALTHCHECK --interval=20s --timeout=8s --start-period=180s --retries=6 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/health', timeout=3)" || exit 1

STOPSIGNAL SIGTERM

ENTRYPOINT ["/opt/workflow-launcher/scripts/start.sh"]
