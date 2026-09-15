# AIMODELKI ALL IN ONE

Launch ComfyUI, JupyterLab, and production-ready model installers in a single RunPod Pod.

## Description

**AIMODELKI ALL IN ONE** provides a simple web launcher that installs the models and custom nodes required by selected ComfyUI workflows. Downloads display live progress, support resuming from `.part` files, and restart ComfyUI automatically after installation.

The optional **Instant Models** connection validates an AIMODELKI token against the central service. The token only selects the download source: catalog buttons use private Cloudflare R2 storage when it is active, or public sources when it is not. R2 credentials are never included in the launcher image.

Built-in installers (no Hugging Face token required):

| Package | Download size |
| --- | ---: |
| Image Generation | 19.9 GiB |
| Dataset Generator | 67.1 GiB |
| Image Edit (support files and custom nodes) | 8.4 GiB |
| Video Motion Control | 26.5 GiB |

Image Edit requires one additional manual file: `flux-2-klein-9b.safetensors` (about 16.9 GiB). Place it at `/workspace/runpod-slim/ComfyUI/models/unet/flux-2-klein-9b.safetensors`, then restart ComfyUI. The launcher does not download this gated file or require a Hugging Face token. Instant Models may alternatively provide it when the active bundle contains the file.

Workflow JSON files are not included. The template installs only models, supporting files, and custom nodes.

## Getting Started

### Requirements

- NVIDIA GPU with enough VRAM for the selected workflow.
- `50 GB` Container Disk for the image and temporary runtime files.
- `250 GB` Volume Disk mounted at `/workspace` for models and resumable downloads.
- Image Edit with FLUX.2 Klein 9B requires approximately `29 GB` VRAM.
- The Volume Disk survives Pod stop/restart and is deleted when the Pod is terminated. Use a Network Volume instead when storage must outlive the Pod.

### Template Configuration

| RunPod setting | Value |
| --- | --- |
| Container image | `aimodelki/aimodelki-allin1:1.5.0` |
| Container Disk | `50 GB` |
| Volume Disk | `250 GB` |
| Volume Mount Path | `/workspace` |
| HTTP Ports | `3000`, `8188`, `8888` |
| Docker Entrypoint | Leave empty |
| Docker Start Command | Leave empty |

Environment variable:

```text
LAUNCHER_UPDATE_ENABLED=0
INSTANT_MODELS_API_URL=https://app.aimodelki.pl/api/v1/instant-models
INSTANT_MODELS_DISK_RESERVE_GB=5
INSTANT_MODELS_DOWNLOAD_CONNECTIONS=64
INSTANT_MODELS_DOWNLOAD_SEGMENT_MB=64
```

No Hugging Face token is embedded in the image or required for automatic built-in downloads. Instant Models requires an AIMODELKI token from the account page.

JupyterLab starts without password or token authentication and opens directly from port `8888`.

### Using the Template

1. Deploy the template as a GPU Pod.
2. Wait until the Pod reports that all services are ready.
3. Open port `3000` from the RunPod **Connect** panel.
4. Select one workflow package and wait for the installation to finish. For Image Edit, add the separately required FLUX file as shown in the launcher.
5. Open ComfyUI using the launcher button or port `8188`.

For Instant Models, copy the token from the AIMODELKI account page, paste it in the launcher, and activate the connection. Then use the same four catalog buttons; with an active token they use R2 automatically, otherwise they use the public catalog URLs. Large R2 objects use 64 concurrent HTTP Range streams by default. Completed segments and resumable `.part` files are stored below `/workspace`.

`INSTANT_MODELS_DOWNLOAD_CONNECTIONS` accepts `1`–`64` and defaults to `64`. Reduce it to `32`, `16` or `8` if a particular route produces repeated R2 `429`/`5xx` responses. `INSTANT_MODELS_DOWNLOAD_SEGMENT_MB` controls the resumable segment size and defaults to `64` MiB.

Only one package can be installed at a time. Other package cards remain locked during an active installation.

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
