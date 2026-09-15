# AIMODELKI ALL IN ONE

Launch ComfyUI, JupyterLab, and production-ready model installers in a single RunPod Pod.

## Description

**AIMODELKI ALL IN ONE** provides a simple web launcher that installs the models and custom nodes required by selected ComfyUI workflows. Downloads display live progress, support resuming from `.part` files, and restart ComfyUI automatically after installation.

The optional **Instant Models** connection validates an AIMODELKI token against the central service. The token only selects the download source: catalog buttons use private Cloudflare R2 storage when it is active, or public sources when it is not. R2 credentials are never included in the launcher image.

Built-in installers (no Hugging Face token required):

| Package | Download size |
| --- | ---: |
| Image Generation | 19.9 GB |
| Dataset Generator | 67.1 GB |
| Image Edit (support files and custom nodes) | 8.4 GB |
| Video Motion Control High Quality | 61.3 GB |
| MiniMax H3 | 63.4 GB |

Image Edit requires one additional manual file: `flux-2-klein-9b.safetensors` (about 16.9 GiB). Place it at `/workspace/runpod-slim/ComfyUI/models/unet/flux-2-klein-9b.safetensors`, then restart ComfyUI. The launcher does not download this gated file or require a Hugging Face token. Instant Models may alternatively provide it when the active bundle contains the file.

Workflow JSON files are not included. The template installs only models, supporting files, and custom nodes.

## How the Pod starts

- **ComfyUI comes from the image.** The stock RunPod start script copies the ComfyUI bundled in `runpod/comfyui` to `/workspace/runpod-slim/ComfyUI` on first start, creates its venv and starts SSH, FileBrowser, JupyterLab and ComfyUI. Nothing is downloaded to get ComfyUI running. The launcher starts next to it on port `3000`.
- **Custom nodes are installed per package.** Choosing a package installs only the nodes it needs, each pinned to one commit. A node comes from its R2 archive when the catalog lists the archive checksum; otherwise only that commit is fetched from GitHub, without history. The Python dependencies of all catalog nodes are already in the image, so `pip` only confirms them. ComfyUI is then restarted through ComfyUI-Manager.
- **Standard Download is a plain download.** Without an Instant Models token, each file comes over one ordinary HTTP request, one file after another, so public hosts such as Hugging Face see normal traffic. An interrupted file resumes with one request from where it stopped.
- **Instant Download is built for speed.** With an active token, large R2 files download with `rangefetch`, a small native downloader built from `tools/rangefetch`: 128 keep-alive range connections per file across every CPU core, each segment written straight to its place in the file, and up to 4 files at once. Small files use aria2c, and the built-in Python downloader remains as a fallback.
- Every file is checked against its SHA-256, and a verified file is never hashed again unless it changes.
- **The image shares its base with `runpod/comfyui:latest`**, the image behind RunPod's own ComfyUI template. A host that already ran that template only pulls this image's own layers (about 0.6 GB).

## GPU and CUDA

| Image tag | CUDA | NVIDIA driver | Supported GPUs |
| --- | --- | --- | --- |
| `1.6.2` | 12.8 | 570 or newer | Volta and newer, for example RTX 5090, RTX PRO 6000, B200, H100, RTX 4090, L40S, L4, RTX 6000 Ada, A100, A40, RTX A6000, RTX 3090, T4, V100 |
| `1.6.2-cu130` | 13.0 | 580 or newer | Turing and newer (no V100) |

If the driver or the GPU does not match the image, `/api/health` reports it under `checks.gpu` and the launcher shows a warning with the reason.

## Getting Started

### Requirements

- NVIDIA GPU with enough VRAM for the selected workflow and a driver that matches the image tag (see above).
- `50 GB` Container Disk for the image and temporary runtime files.
- `250 GB` Volume Disk mounted at `/workspace` for models and resumable downloads.
- Image Edit with FLUX.2 Klein 9B requires approximately `29 GB` VRAM.
- The Volume Disk survives Pod stop/restart and is deleted when the Pod is terminated. Use a Network Volume instead when storage must outlive the Pod.

### Template Configuration

| RunPod setting | Value |
| --- | --- |
| Container image | `aimodelki/aimodelki-allin1:1.6.2` |
| Container Disk | `50 GB` |
| Volume Disk | `250 GB` |
| Volume Mount Path | `/workspace` |
| HTTP Ports | `3000`, `8188`, `8888` |
| Docker Entrypoint | Leave empty |
| Docker Start Command | Leave empty |

Environment variables:

```text
LAUNCHER_UPDATE_ENABLED=0
AIMODELKI_DOWNLOAD_PARALLEL_FILES=4
INSTANT_MODELS_DOWNLOAD_CONNECTIONS=128
INSTANT_MODELS_DOWNLOAD_SEGMENT_MB=16
```

| Variable | Default | Meaning |
| --- | --- | --- |
| `AIMODELKI_DOWNLOAD_PARALLEL_FILES` | `4` | Instant Download: files downloaded at the same time (`1`–`8`). Standard Download always fetches one file at a time. |
| `INSTANT_MODELS_DOWNLOAD_CONNECTIONS` | `128` | Instant Download: connections per R2 file (`1`–`256`). Raise it when a Pod has bandwidth and CPU to spare; reduce it if R2 returns repeated `429`/`5xx` responses. |
| `INSTANT_MODELS_DOWNLOAD_SEGMENT_MB` | `16` | Resumable segment size in MiB. A file uses at most one connection per segment, so smaller segments let smaller files use every connection. |
| `AIMODELKI_DOWNLOADER` | `auto` | Instant Download: `aria2` skips rangefetch; `python` forces the built-in downloader. |
| `AIMODELKI_NODE_ARCHIVE_BASE_URL` | public R2 `custom-nodes/` | Where pinned custom-node archives are downloaded from. |

No Hugging Face token is embedded in the image or required for automatic built-in downloads. Instant Models requires an AIMODELKI token from the account page.

JupyterLab starts without password or token authentication and opens directly from port `8888`.

### Using the Template

1. Deploy the template as a GPU Pod.
2. Wait until the Pod reports that all services are ready.
3. Open port `3000` from the RunPod **Connect** panel.
4. Select one workflow package and wait for the installation to finish. For Image Edit, add the separately required FLUX file as shown in the launcher.
5. Open ComfyUI using the launcher button or port `8188`.

For Instant Models, copy the token from the AIMODELKI account page, paste it in the launcher, and activate the connection. Then use the same catalog buttons; with an active token they use R2 automatically, otherwise they use the public catalog URLs. Completed downloads and resumable `.part` files are stored below `/workspace`.

Only one package can be installed at a time. Other package cards remain locked during an active installation.

## Building the images

```bash
docker build -t aimodelki/aimodelki-allin1:1.6.2 .

docker build \
  --build-arg BASE_IMAGE=runpod/comfyui:1.4.7-cuda13.0@sha256:094dc6d79448b6f118c4d2b054073f92d765c568598e7a96aaeda678a6bcbf3b \
  --build-arg CUDA_VARIANT=cu130 \
  -t aimodelki/aimodelki-allin1:1.6.2-cu130 .
```

`docker/custom-node-requirements.txt` lists the Python dependencies of every catalog node at its pinned revision. Regenerate it whenever a node revision in `catalog/catalog.json` changes. A node directory must use the same revision in every package, which the catalog loader enforces.

### Custom-node archives on R2

```bash
python scripts/export-custom-node-archives.py dist/custom-nodes --write-catalog
```

The script builds one archive per node at its pinned commit, stores the archive SHA-256 in the catalog and prints the upload command. Upload the archives before publishing an image whose catalog references them; until then, nodes come from GitHub.

## Measuring download speed

Run this on the Pod (JupyterLab terminal or SSH) to see whether the network, the disk or the CPU limits downloads:

```bash
python3 /opt/workflow-launcher/scripts/bench-r2.py --instant image-generation
```

It measures rangefetch at 32, 64 and 128 connections into RAM and into `/workspace`, compares curl and Python streams, measures disk writes on `/workspace` and the container disk, and never prints the presigned URL. Add `--full` to download the whole file once with aria2c into each directory.

## Services

| Service | Port |
| --- | ---: |
| AIMODELKI Launcher | `3000` |
| ComfyUI | `8188` |
| JupyterLab | `8888` |

JupyterLab is intentionally accessible without authentication. Anyone who can open the Pod's port `8888` can access its terminal and files.

RunPod proxy URLs follow this format:

```text
https://POD_ID-3000.proxy.runpod.net
https://POD_ID-8188.proxy.runpod.net
https://POD_ID-8888.proxy.runpod.net
```

## Help

If ComfyUI is still initializing, wait a few minutes and check:

```text
https://POD_ID-3000.proxy.runpod.net/api/health
```

A ready Pod returns `"ok": true`. If installation runs out of space, increase the Volume Disk. Interrupted downloads can be resumed by starting the same package again.

Container storage is cleared when the Pod stops. The configured Volume Disk keeps `/workspace` across stops and restarts, until the Pod is terminated.

## RunPod Hub Modes

The Hub image supports two explicit runtime modes:

```text
MODE_TO_RUN=pod
```

starts the interactive launcher, ComfyUI, and JupyterLab. This is the default and recommended mode.

```text
MODE_TO_RUN=serverless
```

starts a lightweight RunPod worker with two metadata actions:

```json
{"input":{"action":"health"}}
```

```json
{"input":{"action":"catalog"}}
```

Model installation and the browser interfaces are available only in Pod mode.

## Authors

**AIMODELKI Team**  
[aimodelki.pl](https://aimodelki.pl/)

## Version History

- **1.6.2**
  - Standard Download (without an Instant Models token) uses one plain request per file and downloads one file at a time; parallel connections and parallel files are reserved for Instant Download.
  - Instant Models API errors name the HTTP status and the server's message, and temporary `502`/`503`/`504` responses or dropped connections are retried.
- **1.6.1**
  - Base image `runpod/comfyui:1.4.7-cuda12.8`, the same layers as `runpod/comfyui:latest`, so Pods start faster on hosts that already ran RunPod's ComfyUI template; CUDA 13.0 moves to the `-cu130` tag.
  - New `rangefetch` downloader for large files: many keep-alive connections across all CPU cores, 128 per file for Instant Models in 16 MiB segments, with resume shared with the Python downloader and automatic renewal of an expired presigned URL.
  - `onnxruntime-gpu` pinned to 1.26.0, the last release built for CUDA 12.
- **1.6.0**
  - CUDA 13.0 base image by default, with a CUDA 12.8 variant for drivers 570–579 and Volta GPUs.
  - ComfyUI starts from the image through the stock RunPod start script instead of a runtime bundle downloaded from R2; SSH and FileBrowser are available again.
  - Custom nodes are installed per package at one pinned commit, from R2 archives or a shallow GitHub fetch; their Python dependencies ship in the image.
  - Model downloads use aria2c with several files at once and built-in SHA-256 verification; verified files are not hashed again.
  - GPU driver and architecture check in `/api/health` and the launcher.
  - Shared custom nodes use one revision across all packages.
- **1.5.0**
  - Added the Video Motion Control High Quality and MiniMax H3 packages.
- **1.4.0**
  - Public ComfyUI runtime bootstrap from Cloudflare R2.
  - Persistent, verified runtime with resumable download.
- **1.3.5**
  - Simplified Instant Models status, improved catalog download buttons, and added an explicit resume action after a cancelled or interrupted installation.
- **1.3.4**
  - Enforced one catalog-only installation flow: token activation selects Instant Download, disconnected mode selects Standard Download, and an incomplete Instant manifest can no longer fall back to public sources.
- **1.3.3**
  - Unified catalog installation: an active Instant Models token switches catalog downloads to R2; the separate Sync Models flow was removed.
- **1.3.2**
  - Increased the Instant Models connection limit and default from 32 to 64.
- **1.3.1**
  - Increased the default Instant Models parallel download count from 8 to 32.
- **1.3.0**
  - Added parallel R2 Range downloads with segment-level resume and an eight-connection default; configured a 250 GB RunPod Volume Disk at `/workspace`.
- **1.2.1**
  - Restored Image Edit support-file installer with an explicit manual FLUX requirement; moved Instant Models above the catalog and added a purchase-inquiry button.
- **1.2.0**
  - Added Instant Models token activation, private R2 synchronization, disk checks, retry, resume, and SHA-256 verification. Removed the embedded Hugging Face token and moved Image Edit to Instant Models only.
- **1.1.0**
  - Added RunPod Hub metadata, automated tests, and Pod/Serverless dual-mode startup.
- **1.0.1**
  - JupyterLab now opens without password or token authentication.
- **1.0.0**
  - Initial public RunPod release.
