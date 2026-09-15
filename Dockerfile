# syntax=docker/dockerfile:1.7
#
# Default runtime: RunPod ComfyUI 1.4.7 on CUDA 12.8, the image behind runpod/comfyui:latest
# and RunPod's own ComfyUI template. Sharing its layers lets hosts that already ran that
# template pull only this image's own layers. Needs NVIDIA driver 570 or newer and
# supports Volta through Blackwell (V100 to RTX 5090). Build the CUDA 13.0 variant with:
#   --build-arg BASE_IMAGE=runpod/comfyui:1.4.7-cuda13.0@sha256:094dc6d79448b6f118c4d2b054073f92d765c568598e7a96aaeda678a6bcbf3b
#   --build-arg CUDA_VARIANT=cu130
ARG BASE_IMAGE=runpod/comfyui:1.4.7-cuda12.8@sha256:2cb4015beb6e16b0bbc05ed5d1e39288545b7f4ce8fed9534f4ef0fa88aa2e4d
ARG GO_IMAGE=golang:1.27.1-bookworm@sha256:648f440f42a0958804efb24df176f806f9d353b41f1c0627f666428e40310f6b

# rangefetch: multi-core, many-connection range downloader for large model files.
FROM ${GO_IMAGE} AS rangefetch
WORKDIR /src
COPY tools/rangefetch/ ./
RUN go vet ./... \
    && go test -count=1 ./... \
    && CGO_ENABLED=0 go build -trimpath -ldflags="-s -w" -o /out/rangefetch .

FROM ${BASE_IMAGE}

ARG CUDA_VARIANT=cu128

LABEL org.opencontainers.image.title="AIMODELKI ALL IN ONE" \
      org.opencontainers.image.description="ComfyUI, JupyterLab, workflow installers and Instant Models for RunPod" \
      org.opencontainers.image.version="1.6.5"

USER root

# aria2 downloads small Instant Models files. The stock RunPod start script is kept and run next to
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
# onnxruntime-gpu 1.27 and newer are built for CUDA 13, so the CUDA 12.8 image pins 1.26.
COPY docker/custom-node-requirements.txt /tmp/custom-node-requirements.txt
RUN set -eu; \
    python3 -m pip freeze \
      | grep -iE '^(torch|torchvision|torchaudio|numpy|pillow|transformers|opencv-python|opencv-python-headless)==' \
      > /opt/aimodelki-node-constraints.txt; \
    sed -n 's/^opencv-python==/opencv-contrib-python==/p' /opt/aimodelki-node-constraints.txt \
      >> /opt/aimodelki-node-constraints.txt; \
    if [ "$CUDA_VARIANT" = "cu128" ]; then \
      echo "onnxruntime-gpu==1.26.0" >> /opt/aimodelki-node-constraints.txt; \
    fi; \
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
    INSTANT_MODELS_DOWNLOAD_CONNECTIONS=48 \
    INSTANT_MODELS_DOWNLOAD_SEGMENT_MB=64 \
    INSTANT_MODELS_DISK_RESERVE_GB=5 \
    AIMODELKI_DOWNLOAD_PARALLEL_FILES=1

EXPOSE 3000 8188 8888

# Frequently changing files come last, so they stay small layers.
COPY --from=rangefetch /out/rangefetch /usr/local/bin/rangefetch
COPY docker ./docker
COPY catalog ./catalog
COPY --chmod=0755 scripts ./scripts
COPY launcher ./launcher

HEALTHCHECK --interval=20s --timeout=8s --start-period=180s --retries=6 \
  CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:3000/api/health', timeout=3)" || exit 1

STOPSIGNAL SIGTERM

ENTRYPOINT ["/opt/workflow-launcher/scripts/start.sh"]
