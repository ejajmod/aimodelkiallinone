"""NVIDIA driver and GPU architecture checks for the CUDA build baked into the image."""

from __future__ import annotations

import os
import re
import subprocess
import threading
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class CudaRuntime:
    variant: str
    cuda: str
    min_driver: int
    min_capability: tuple[int, int]


# Driver minimums come from the NVIDIA CUDA release notes; the capabilities are the
# oldest architectures compiled into the base image's PyTorch wheels (cu130 dropped Volta).
RUNTIMES = {
    "cu130": CudaRuntime("cu130", "13.0", 580, (7, 5)),
    "cu128": CudaRuntime("cu128", "12.8", 570, (7, 0)),
}
QUERY = ["nvidia-smi", "--query-gpu=name,driver_version,compute_cap", "--format=csv,noheader"]


@dataclass(frozen=True)
class GpuDevice:
    name: str
    driver: str
    capability: tuple[int, int] | None


def current_runtime() -> CudaRuntime:
    return RUNTIMES.get(os.getenv("AIMODELKI_CUDA_VARIANT", "cu128").strip().lower(), RUNTIMES["cu128"])


def parse_nvidia_smi(output: str) -> list[GpuDevice]:
    devices: list[GpuDevice] = []
    for line in output.splitlines():
        parts = [part.strip() for part in line.split(",")]
        if len(parts) < 2 or not parts[0]:
            continue
        capability = None
        if len(parts) >= 3:
            match = re.fullmatch(r"(\d+)\.(\d+)", parts[2])
            if match:
                capability = (int(match.group(1)), int(match.group(2)))
        devices.append(GpuDevice(parts[0], parts[1], capability))
    return devices


def query_devices(run: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> list[GpuDevice] | None:
    """GPUs reported by nvidia-smi, or None when that cannot be determined."""
    try:
        result = run(QUERY, capture_output=True, text=True, timeout=10, check=False)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    return parse_nvidia_smi(result.stdout)


def evaluate(runtime: CudaRuntime, devices: list[GpuDevice] | None) -> dict[str, object]:
    payload: dict[str, object] = {
        "cuda": runtime.cuda,
        "variant": runtime.variant,
        "min_driver": runtime.min_driver,
        "detected": devices is not None,
        "devices": [
            {
                "name": device.name,
                "driver": device.driver,
                "compute_capability": ".".join(map(str, device.capability)) if device.capability else None,
            }
            for device in devices or []
        ],
    }
    if devices is None:
        # Without nvidia-smi nothing can be said; ComfyUI's own CUDA check still applies.
        payload.update(ok=True, problems=[])
        return payload

    problems: list[str] = []
    if not devices:
        problems.append("Nie wykryto karty NVIDIA. Uruchom Poda z GPU.")
    for device in devices:
        match = re.match(r"\d+", device.driver)
        driver_major = int(match.group()) if match else 0
        if driver_major < runtime.min_driver:
            advice = (
                " Wybierz Poda z nowszym sterownikiem albo wariant obrazu z CUDA 12.8."
                if runtime.variant == "cu130" and driver_major >= RUNTIMES["cu128"].min_driver
                else " Wybierz Poda z nowszym sterownikiem."
            )
            problems.append(
                f"{device.name}: sterownik NVIDIA {device.driver} jest za stary dla CUDA {runtime.cuda} "
                f"(wymagany {runtime.min_driver} lub nowszy).{advice}"
            )
        if device.capability and device.capability < runtime.min_capability:
            problems.append(
                f"{device.name}: architektura GPU {device.capability[0]}.{device.capability[1]} "
                f"nie jest obsługiwana przez PyTorch dla CUDA {runtime.cuda}."
            )
    payload.update(ok=not problems, problems=problems)
    return payload


_cache: dict[str, object] | None = None
_cache_lock = threading.Lock()


def status() -> dict[str, object]:
    """GPU compatibility for this Pod; the hardware does not change while it runs."""
    global _cache
    with _cache_lock:
        if _cache is None:
            _cache = evaluate(current_runtime(), query_devices())
        return dict(_cache)


def reset_cache() -> None:
    global _cache
    with _cache_lock:
        _cache = None
