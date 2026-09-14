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
    assert template["env"]["INSTANT_MODELS_DOWNLOAD_CONNECTIONS"] == "64"


def test_no_hugging_face_token_is_baked_into_launcher() -> None:
    launcher_source = (ROOT / "launcher" / "app.py").read_text(encoding="utf-8")
    assert "PUBLIC_HF_TOKEN" not in launcher_source
    assert not re.search(r"hf_[A-Za-z0-9]{20,}", launcher_source)
