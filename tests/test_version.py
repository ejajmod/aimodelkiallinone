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
    assert template["env"]["AIMODELKI_DOWNLOAD_CONNECTIONS_PER_FILE"] == "16"
    assert not any(key.startswith("COMFYUI_BUNDLE") for key in template["env"])


def test_image_runs_stock_comfyui_on_cuda_13() -> None:
    dockerfile = (ROOT / "Dockerfile").read_text(encoding="utf-8")
    assert (
        "runpod/comfyui:1.4.6-cuda13.0@sha256:0bf75436da591e0f26d299af3741e07cb8ce8ce36566d1a7d8d78aae458e5d67"
        in dockerfile
    )
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
