"""Bake catalog custom nodes into the ComfyUI bundle supplied by RunPod."""

import json
import subprocess
import sys
from pathlib import Path


def run(*command: str, cwd: Path | None = None) -> None:
    subprocess.run(command, cwd=cwd, check=True)


def bake(catalog_path: Path, comfyui_root: Path) -> None:
    if not (comfyui_root / "main.py").is_file():
        raise RuntimeError(f"RunPod base image has no baked ComfyUI at {comfyui_root}")

    catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
    seen: dict[str, str] = {}
    constraints = Path("/opt/comfyui-runtime-constraints.txt")
    for workflow in catalog["workflows"]:
        for node in workflow.get("custom_nodes", []):
            relative = Path(node["directory"])
            if relative.parts[0] != "custom_nodes" or ".." in relative.parts:
                raise ValueError(f"Invalid custom node destination: {relative}")
            revision = node["revision"]
            key = str(relative)
            if key in seen:
                if seen[key] != revision:
                    print(f"{key}: another catalog revision is installed on demand", flush=True)
                continue
            seen[key] = revision
            destination = comfyui_root / relative
            if destination.exists():
                raise RuntimeError(f"Custom node already exists in base image: {destination}")

            run("git", "clone", "--filter=blob:none", node["repository"], str(destination))
            run("git", "fetch", "--depth", "1", "origin", revision, cwd=destination)
            run("git", "checkout", "--detach", "FETCH_HEAD", cwd=destination)
            actual = subprocess.check_output(
                ["git", "rev-parse", "HEAD"], cwd=destination, text=True
            ).strip()
            if actual != revision:
                raise RuntimeError(f"Revision mismatch for {relative}: {actual}")

            requirements = destination / "requirements.txt"
            if node.get("install_requirements") and requirements.is_file():
                command = [
                    sys.executable,
                    "-m",
                    "pip",
                    "install",
                    "--no-cache-dir",
                    "-r",
                    str(requirements),
                ]
                # NVIDIA VFX is distributed through NVIDIA's wheel-stub build
                # backend, which must run in an isolated build environment.
                if "nvidia-vfx" not in requirements.read_text(encoding="utf-8").lower():
                    command.insert(5, "--no-build-isolation")
                if constraints.is_file():
                    command.extend(["-c", str(constraints)])
                run(*command, cwd=destination)

            # Experimental isolated runtimes (currently SAM3/comfy-env) are
            # GPU-sensitive and can exhaust Docker Desktop during a portable
            # multi-GPU build. Their source and ordinary requirements are baked,
            # while the isolated runtime is finalized when its workflow is chosen.
            install_script = destination / (node.get("install_script") or "")
            marker_name = (
                ".aimodelki-baked-source-revision"
                if install_script.is_file() and any(destination.rglob("comfy-env.toml"))
                else ".aimodelki-baked-revision"
            )
            (destination / marker_name).write_text(revision + "\n", encoding="ascii")

    print(f"Baked {len(seen)} catalog custom nodes into {comfyui_root}", flush=True)


if __name__ == "__main__":
    bake(Path(sys.argv[1]), Path(sys.argv[2]))
