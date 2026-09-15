"""Keep the launcher, image label, and RunPod template on one version."""

import json
import re
from pathlib import Path

from launcher import __version__


ROOT = Path(__file__).resolve().parents[1]


def test_image_version_matches_launcher_and_template() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    match = re.search(r'org\.opencontainers\.image\.version="([^"]+)"', dockerfile)
    assert match is not None
    assert match.group(1) == __version__

    template = json.loads((ROOT / "runpod-template.example.json").read_text(encoding="utf-8"))
    assert template["imageName"].endswith(f":{__version__}")
    assert template["volumeInGb"] == 250
    assert template["volumeMountPath"] == "/workspace"
    assert template["env"]["AIMODELKI_DOWNLOAD_PARALLEL_FILES"] == "4"
    assert template["env"]["INSTANT_MODELS_DOWNLOAD_CONNECTIONS"] == "64"
    # Standard Download has no connection setting: it always uses one request per file.
    assert "AIMODELKI_DOWNLOAD_CONNECTIONS_PER_FILE" not in template["env"]
    assert not any(key.startswith("COMFYUI_BUNDLE") for key in template["env"])


def test_image_shares_layers_with_runpod_comfyui_latest() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    # runpod/comfyui:latest is 1.4.7-cuda12.8; its layers are the likeliest to be cached on hosts.
    assert (
        "ARG BASE_IMAGE=runpod/comfyui:1.4.7-cuda12.8@sha256:2cb4015beb6e16b0bbc05ed5d1e39288545b7f4ce8fed9534f4ef0fa88aa2e4d"
        in dockerfile
    )
    assert "ARG CUDA_VARIANT=cu128" in dockerfile
    assert "onnxruntime-gpu==1.26.0" in dockerfile
    assert "COPY --from=rangefetch /out/rangefetch /usr/local/bin/rangefetch" in dockerfile
    assert "COMFYUI_BUNDLE" not in dockerfile
    assert "bake-custom-nodes" not in dockerfile
    # Launcher code is the last layer, so a UI change does not invalidate the dependencies.
    assert dockerfile.index("COPY launcher") > dockerfile.index("custom-node-requirements.txt")

    start = (ROOT / "scripts" / "start.sh").read_text(encoding="utf-8")
    assert "runpod-base-start.sh" in start
    assert "bootstrap-comfyui" not in start


def test_no_hugging_face_token_is_baked_into_launcher() -> None:
    launcher_source = (ROOT / "launcher" / "app.py").read_text(encoding="utf-8")
    assert "PUBLIC_HF_TOKEN" not in launcher_source
    assert not re.search(r"hf_[A-Za-z0-9]{20,}", launcher_source)
