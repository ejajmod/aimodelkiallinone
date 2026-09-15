"""Measure where model download time goes on a Pod: network, parallelism, disk and CPU.

Run it from JupyterLab or SSH on the Pod:

    python3 /opt/workflow-launcher/scripts/bench-r2.py --instant image-generation
    python3 /opt/workflow-launcher/scripts/bench-r2.py --url "https://..." --size-mib 4096

``--instant`` asks the Instant Models API (with the token saved by the launcher) for a
presigned URL of the largest file in that package. The URL is never printed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import requests

APP_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP_ROOT))

MIB = 1024 * 1024


def instant_url(workflow_id: str) -> tuple[str, int]:
    from launcher.instant_models import InstantModelsManager

    manager = InstantModelsManager(
        Path(os.getenv("INSTANT_MODELS_STATE_DIR", "/workspace/.instant-models")),
        os.getenv("INSTANT_MODELS_API_URL", "https://app.aimodelki.pl/api/v1/instant-models"),
    )
    source = manager.source_for(workflow_id)
    if source is None:
        raise SystemExit("Instant Models is not activated in the launcher.")
    largest = max(source.bundle.files, key=lambda item: item.size)
    return source.client.download_url(largest.id), largest.size


def curl_config(url: str, start: int, end: int) -> str:
    # Passed on stdin so the presigned signature stays out of the process list.
    return f'url = "{url}"\nrange = "{start}-{end}"\noutput = "/dev/null"\nsilent\nfail\n'


def curl_ranges(url: str, total: int, streams: int) -> float:
    chunk = total // streams
    processes = []
    started = time.monotonic()
    for index in range(streams):
        start = index * chunk
        end = total - 1 if index == streams - 1 else start + chunk - 1
        process = subprocess.Popen(["curl", "--config", "-"], stdin=subprocess.PIPE, text=True)
        process.stdin.write(curl_config(url, start, end))
        process.stdin.close()
        processes.append(process)
    failures = sum(1 for process in processes if process.wait() != 0)
    elapsed = time.monotonic() - started
    if failures:
        print(f"  ! {failures} of {streams} curl streams failed")
    return total / MIB / elapsed


def python_ranges(url: str, total: int, streams: int) -> float:
    chunk = total // streams

    def fetch(index: int) -> None:
        start = index * chunk
        end = total - 1 if index == streams - 1 else start + chunk - 1
        with requests.get(url, headers={"Range": f"bytes={start}-{end}"}, stream=True, timeout=(20, 120)) as response:
            response.raise_for_status()
            for _ in response.iter_content(4 * MIB):
                pass

    threads = [threading.Thread(target=fetch, args=(index,)) for index in range(streams)]
    started = time.monotonic()
    cpu_started = time.process_time()
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    elapsed = time.monotonic() - started
    cpu = (time.process_time() - cpu_started) / elapsed * 100
    print(f"  python {streams:>2} streams used {cpu:.0f}% of one CPU core")
    return total / MIB / elapsed


def disk_write(directory: Path, total: int) -> float | None:
    try:
        directory.mkdir(parents=True, exist_ok=True)
        target = Path(tempfile.mkstemp(prefix=".aimodelki-bench-", dir=directory)[1])
    except OSError as exc:
        print(f"  ! {directory}: {exc}")
        return None
    block = os.urandom(16 * MIB)
    started = time.monotonic()
    try:
        with target.open("wb", buffering=0) as handle:
            written = 0
            while written < total:
                written += handle.write(block)
            os.fsync(handle.fileno())
        return written / MIB / (time.monotonic() - started)
    finally:
        target.unlink(missing_ok=True)


def aria2_full(url: str, directory: Path, connections: int) -> float | None:
    if not shutil.which("aria2c"):
        print("  ! aria2c is not installed")
        return None
    output = directory / ".aimodelki-bench-aria2.bin"
    started = time.monotonic()
    process = subprocess.run(
        [
            "aria2c", "--no-conf=true", "--input-file=-", f"--dir={directory}", f"--out={output.name}",
            f"--max-connection-per-server={connections}", f"--split={connections}", "--min-split-size=4M",
            "--file-allocation=none", "--allow-overwrite=true", "--console-log-level=warn",
            "--summary-interval=0", "--download-result=hide",
        ],
        input=url + "\n",
        text=True,
        capture_output=True,
    )
    elapsed = time.monotonic() - started
    try:
        if process.returncode:
            print(f"  ! aria2c exited with {process.returncode}")
            return None
        return output.stat().st_size / MIB / elapsed
    finally:
        output.unlink(missing_ok=True)
        output.with_name(output.name + ".aria2").unlink(missing_ok=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--url", help="presigned or public URL of a large file")
    source.add_argument("--instant", metavar="WORKFLOW_ID", help="package id from the catalog")
    parser.add_argument("--size-mib", type=int, default=4096, help="bytes read per network test")
    parser.add_argument("--dirs", nargs="+", type=Path, default=[Path("/workspace"), Path("/root")])
    parser.add_argument("--full", action="store_true", help="also download the whole file with aria2c into each directory")
    args = parser.parse_args()

    if args.instant:
        url, size = instant_url(args.instant)
    else:
        url = args.url
        size = int(requests.head(url, allow_redirects=True, timeout=20).headers.get("Content-Length", 0) or 0)
    total = min(args.size_mib * MIB, size) if size else args.size_mib * MIB

    print(f"vCPU: {os.cpu_count()}  load: {' '.join(f'{value:.1f}' for value in os.getloadavg())}")
    for directory in args.dirs:
        if directory.exists():
            usage = shutil.disk_usage(directory)
            print(f"{directory}: {usage.free / 1024**3:.0f} GiB free")

    timing = subprocess.run(
        ["curl", "--config", "-", "--write-out", "%{time_connect} %{time_appconnect}"],
        input=curl_config(url, 0, 0),
        capture_output=True,
        text=True,
    ).stdout.split()
    if len(timing) == 2:
        print(f"TCP connect {float(timing[0]) * 1000:.0f} ms, TLS ready {float(timing[1]) * 1000:.0f} ms")

    print(f"\nNetwork only, {total // MIB} MiB discarded:")
    for streams in (1, 16, 64):
        print(f"  curl   {streams:>2} streams: {curl_ranges(url, total, streams):8.0f} MiB/s")
    for streams in (16, 64):
        print(f"  python {streams:>2} streams: {python_ranges(url, total, streams):8.0f} MiB/s")

    print("\nDisk write with fsync:")
    for directory in args.dirs:
        speed = disk_write(directory, min(total, 4096 * MIB))
        if speed is not None:
            print(f"  {directory}: {speed:8.0f} MiB/s")

    if args.full:
        print("\naria2c, whole file, 16 connections:")
        for directory in args.dirs:
            speed = aria2_full(url, directory, 16)
            if speed is not None:
                print(f"  {directory}: {speed:8.0f} MiB/s")

    print(
        "\nReading: if curl 64 streams is far above python 64 streams, the CPU limits the Python "
        "downloader; if the disk write is below the network figure, the disk is the bottleneck."
    )


if __name__ == "__main__":
    main()
