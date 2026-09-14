"""Install the public, versioned ComfyUI runtime bundle into /workspace."""

from __future__ import annotations

import hashlib
import os
import shutil
import sys
import tarfile
import time
from pathlib import Path

import requests


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_extract(archive: Path, destination: Path) -> None:
    destination = destination.resolve()
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            target = (destination / member.name).resolve()
            if target != destination and destination not in target.parents:
                raise RuntimeError(f"Unsafe archive member: {member.name}")
        bundle.extractall(destination, filter="data")


def download(url: str, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(6):
        offset = destination.stat().st_size if destination.exists() else 0
        headers = {"Range": f"bytes={offset}-"} if offset else {}
        try:
            with requests.get(url, headers=headers, stream=True, timeout=(15, 120)) as response:
                # The .part is already complete; the SHA-256 check decides whether it is usable.
                if offset and response.status_code == 416:
                    return
                if offset and response.status_code == 200:
                    offset = 0
                    destination.unlink(missing_ok=True)
                response.raise_for_status()
                mode = "ab" if offset and response.status_code == 206 else "wb"
                with destination.open(mode) as handle:
                    for chunk in response.iter_content(8 * 1024 * 1024):
                        if chunk:
                            handle.write(chunk)
            return
        except (requests.RequestException, OSError):
            if attempt == 5:
                raise
            time.sleep(min(2**attempt, 16))


def main() -> int:
    root = Path(os.environ["COMFYUI_ROOT"])
    version = os.environ["COMFYUI_BUNDLE_VERSION"]
    expected = os.environ["COMFYUI_BUNDLE_SHA256"].lower()
    url = os.environ["COMFYUI_BUNDLE_URL"]
    marker = root / ".aimodelki-runtime-version"

    if (root / "main.py").is_file() and marker.is_file() and marker.read_text().strip() == version:
        print(f"AIMODELKI: ComfyUI runtime {version} is already ready", flush=True)
        return 0

    cache = root.parent / ".runtime-cache"
    archive = cache / f"comfyui-{version}.tar.gz.part"
    staging = root.parent / f".ComfyUI-{version}.staging"
    print(f"AIMODELKI: downloading public ComfyUI runtime {version}", flush=True)
    download(url, archive)
    actual = sha256(archive)
    if actual != expected:
        archive.unlink(missing_ok=True)
        raise RuntimeError(f"ComfyUI bundle SHA-256 mismatch: {actual}")

    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    safe_extract(archive, staging)
    extracted = staging / "comfyui-baked"
    if not (extracted / "main.py").is_file():
        raise RuntimeError("ComfyUI bundle does not contain comfyui-baked/main.py")
    (extracted / ".aimodelki-runtime-version").write_text(version + "\n", encoding="ascii")

    if root.exists():
        # Preserve user-owned data when replacing an older runtime.
        for name in ("models", "input", "output", "user"):
            source = root / name
            target = extracted / name
            if source.exists():
                if target.exists():
                    shutil.rmtree(target)
                shutil.move(str(source), str(target))
        old_nodes = root / "custom_nodes"
        new_nodes = extracted / "custom_nodes"
        if old_nodes.is_dir():
            new_nodes.mkdir(parents=True, exist_ok=True)
            for node in old_nodes.iterdir():
                if not (new_nodes / node.name).exists():
                    shutil.move(str(node), str(new_nodes / node.name))
        backup = root.parent / ".ComfyUI.previous"
        shutil.rmtree(backup, ignore_errors=True)
        root.replace(backup)
    extracted.replace(root)
    shutil.rmtree(staging, ignore_errors=True)
    shutil.rmtree(root.parent / ".ComfyUI.previous", ignore_errors=True)
    archive.unlink(missing_ok=True)
    print(f"AIMODELKI: ComfyUI runtime {version} installed", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(f"AIMODELKI: ComfyUI bootstrap failed: {exc}", file=sys.stderr, flush=True)
        raise
