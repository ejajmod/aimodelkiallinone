import threading
import time
from pathlib import Path

import pytest

from launcher.catalog import DownloadSpec
from launcher.download_engine import DownloadEngine, DownloadRequest
from launcher.installer import InstallCancelled, InstallError, Installer, ResolvedDownload
from launcher.state import StateStore


def resolved(name: str, size: int, instant: bool = True) -> ResolvedDownload:
    spec = DownloadSpec(
        name=name,
        url=f"https://example.invalid/{name}",
        destination=f"models/{name}",
        size_bytes=size,
        sha256="a" * 64,
    )
    return ResolvedDownload(spec, lambda: spec.url, instant=instant)


def make_installer(tmp_path) -> Installer:
    return Installer(tmp_path / "ComfyUI", StateStore(tmp_path / "state"), tmp_path / "restart.sh")


def track_concurrency(installer: Installer, monkeypatch) -> list[int]:
    guard = threading.Lock()
    active = 0
    peak = [0]

    def fake_download(item, report, cancelled):
        nonlocal active
        with guard:
            active += 1
            peak[0] = max(peak[0], active)
        size = item.spec.size_bytes
        report(size // 2, size, 10)
        time.sleep(0.05)
        report(size, size, 20)
        with guard:
            active -= 1
        return size

    monkeypatch.setattr(installer, "_download_one", fake_download)
    return peak


def test_instant_files_download_concurrently_with_aggregate_progress(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIMODELKI_DOWNLOAD_PARALLEL_FILES", "3")
    installer = make_installer(tmp_path)
    peak = track_concurrency(installer, monkeypatch)

    installer._download_many([resolved(f"file-{index}.bin", 100 * (index + 1)) for index in range(5)], total_items=5)

    state = installer.store.get()
    assert peak[0] >= 2
    assert state["downloaded_bytes"] == state["total_bytes"] == 1500
    assert state["current_index"] == 5
    assert state["progress"] == 90


def test_standard_download_fetches_one_file_after_another(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIMODELKI_DOWNLOAD_PARALLEL_FILES", "8")
    installer = make_installer(tmp_path)
    peak = track_concurrency(installer, monkeypatch)

    installer._download_many([resolved(f"file-{index}.bin", 100, instant=False) for index in range(4)], total_items=4)

    assert peak[0] == 1
    assert installer.store.get()["downloaded_bytes"] == 400


def test_standard_download_uses_one_request_per_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIMODELKI_DOWNLOADER", "auto")
    monkeypatch.setattr("launcher.installer.shutil.which", lambda name: f"/usr/local/bin/{name}")
    installer = make_installer(tmp_path)

    standard = installer.download_engine
    assert standard.rangefetch is None
    assert standard.aria2c is None
    assert standard.parallelism == 1
    assert installer._connection_count(instant=False) == 1

    instant = installer.instant_download_engine
    assert instant.rangefetch == "/usr/local/bin/rangefetch"
    assert installer._connection_count(instant=True) == 4 * 128


def test_single_stream_restarts_a_segmented_partial_file(tmp_path, monkeypatch) -> None:
    engine = DownloadEngine(tmp_path)
    part = tmp_path / "models" / "model.bin.part"
    part.parent.mkdir(parents=True)
    part.write_bytes(b"\0" * 64)
    part.with_name(part.name + ".segments.json").write_text("{}", encoding="utf-8")
    seen: list[bool] = []

    def transfer(item, target_part, cancelled, progress):
        seen.append(target_part.exists())
        target_part.write_bytes(b"x" * 64)

    monkeypatch.setattr(engine, "_transfer", transfer)

    engine.download(
        DownloadRequest("Model", "models/model.bin", 64, None, lambda: "https://example.invalid/model.bin"),
        cancelled=lambda: False,
        progress=lambda *_args: None,
    )

    assert seen == [False]
    assert not part.with_name(part.name + ".segments.json").exists()
    assert (tmp_path / "models" / "model.bin").read_bytes() == b"x" * 64


def test_failed_file_stops_the_other_transfers(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("AIMODELKI_DOWNLOAD_PARALLEL_FILES", "2")
    installer = make_installer(tmp_path)
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
