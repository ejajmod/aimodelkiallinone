# AIMODELKI ALL IN ONE

Launch ComfyUI, JupyterLab, and production-ready model installers in a single RunPod Pod.

## Description

**AIMODELKI ALL IN ONE** provides a simple web launcher that installs the models and custom nodes required by selected ComfyUI workflows. Downloads display live progress, support resuming from `.part` files, and restart ComfyUI automatically after installation.

Available packages:

| Package | Download size |
| --- | ---: |
| Image Generation | 19.9 GiB |
| Dataset Generator | 67.1 GiB |
| Image Edit | 25.3 GiB |
| Video Motion Control | 26.5 GiB |

Workflow JSON files are not included. The template installs only models, supporting files, and custom nodes.

## Getting Started

### Requirements

- NVIDIA GPU with enough VRAM for the selected workflow.
- `150 GB` Container Disk recommended.
- Image Edit with FLUX.2 Klein 9B requires approximately `29 GB` VRAM.
- No persistent volume is required. Attach one at `/workspace` only if models must survive Pod deletion.

### Template Configuration

| RunPod setting | Value |
| --- | --- |
| Container image | `aimodelki/aimodelki-allin1:1.0.1` |
| Container Disk | `150 GB` |
| Volume Disk | `0 GB` or optional persistent volume |
| Volume Mount Path | `/workspace` |
| HTTP Ports | `3000`, `8188`, `8888` |
| Docker Entrypoint | Leave empty |
| Docker Start Command | Leave empty |

Environment variable:

```text
LAUNCHER_UPDATE_ENABLED=0
```

No user-provided Hugging Face token is required.

JupyterLab starts without password or token authentication and opens directly from port `8888`.

### Using the Template

1. Deploy the template as a GPU Pod.
2. Wait until the Pod reports that all services are ready.
3. Open port `3000` from the RunPod **Connect** panel.
4. Select one workflow package and wait for the installation to finish.
5. Open ComfyUI using the launcher button or port `8188`.

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

A ready Pod returns `"ok": true`. If installation runs out of space, increase the Container Disk. Interrupted downloads can be resumed by starting the same package again.

Container storage is deleted with the Pod. Use a persistent volume mounted at `/workspace` if downloaded models must be retained.

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

- **1.1.0**
  - Added RunPod Hub metadata, automated tests, and Pod/Serverless dual-mode startup.
- **1.0.1**
  - JupyterLab now opens without password or token authentication.
- **1.0.0**
  - Initial public RunPod release.
