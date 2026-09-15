import threading
import time

import pytest

from launcher.catalog import DownloadSpec
from launcher.installer import InstallCancelled, InstallError, Installer, ResolvedDownload
from launcher.state import StateStore


def resolved(name: str, size: int) -> ResolvedDownload:
    spec = DownloadSpec(
        name=name,
        url=f"https://example.invalid/{name}",
        destination=f"models/{name}",
        size_bytes=size,
        sha256="a" * 64,
    )
    return ResolvedDownload(spec, lambda: spec.url)


def test_files_download_concurrently_with_aggregate_progress(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIMODELKI_DOWNLOAD_PARALLEL_FILES", "3")
    installer = Installer(tmp_path / "ComfyUI", StateStore(tmp_path / "state"), tmp_path / "restart.sh")
    guard = threading.Lock()
    active = 0
    peak = 0

    def fake_download(item, report, cancelled):
        nonlocal active, peak
        with guard:
            active += 1
            peak = max(peak, active)
        size = item.spec.size_bytes
        report(size // 2, size, 10)
        time.sleep(0.05)
        report(size, size, 20)
        with guard:
            active -= 1
        return size

    monkeypatch.setattr(installer, "_download_one", fake_download)

    installer._download_many([resolved(f"file-{index}.bin", 100 * (index + 1)) for index in range(5)], total_items=5)

    state = installer.store.get()
    assert peak >= 2
    assert state["downloaded_bytes"] == state["total_bytes"] == 1500
    assert state["current_index"] == 5
    assert state["progress"] == 90


def test_failed_file_stops_the_other_transfers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIMODELKI_DOWNLOAD_PARALLEL_FILES", "2")
    installer = Installer(tmp_path / "ComfyUI", StateStore(tmp_path / "state"), tmp_path / "restart.sh")
    stopped = threading.Event()

    def fake_download(item, report, cancelled):
        if item.spec.name == "broken.bin":
            time.sleep(0.05)
            raise InstallError("HTTP 403")
        while not cancelled():
            time.sleep(0.01)
        stopped.set()
        raise InstallCancelled()

    monkeypatch.setattr(installer, "_download_one", fake_download)

    with pytest.raises(InstallError, match="403"):
        installer._download_many([resolved("slow.bin", 10), resolved("broken.bin", 10)], total_items=2)

    assert stopped.is_set()
