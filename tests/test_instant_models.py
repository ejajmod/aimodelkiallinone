import hashlib
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import requests
import pytest

from launcher.catalog import load_catalog
from launcher.instant_models import (
    InstantBundle,
    InstantFile,
    InstantManifest,
    InstantModelsManager,
    InstantDownloadSource,
)
from launcher.download_engine import DownloadEngine, DownloadError, DownloadRequest
from launcher.installer import InstallError, Installer
from launcher.operations import OperationBusy, OperationCoordinator
from launcher.state import StateStore


class FakeClient:
    def __init__(self, manifest, url):
        self._manifest = manifest
        self._url = url

    def manifest(self):
        return self._manifest

    def download_url(self, _file_id):
        return self._url


def test_coordinator_allows_only_one_model_writer() -> None:
    coordinator = OperationCoordinator()
    coordinator.acquire("workflow")
    try:
        try:
            coordinator.acquire("instant-models")
            assert False, "second operation should be rejected"
        except OperationBusy:
            pass
    finally:
        coordinator.release("workflow")


def test_activation_persists_token_without_returning_plain_value(tmp_path) -> None:
    manifest = InstantManifest(version="v1", valid_until="2030-01-01T00:00:00Z", bundles=())
    coordinator = OperationCoordinator()
    manager = InstantModelsManager(
        tmp_path / "state",
        "https://example.invalid/api/v1/instant-models",
        client_factory=lambda _token: FakeClient(manifest, "https://example.invalid/file"),
    )

    state = manager.activate("im_live_" + "a" * 43)

    assert state["connected"] is True
    assert "a" * 43 not in str(state)
    assert manager.credentials.load() == "im_live_" + "a" * 43


def test_active_token_resolves_catalog_bundle_by_destination(tmp_path) -> None:
    file = InstantFile("model-1", "model.safetensors", 123, "a" * 64, "diffusion_models/model.safetensors")
    manifest = InstantManifest("v1", "2030-01-01T00:00:00Z", (InstantBundle("image-generation", "Image Generation", "", 123, (file,)),))
    manager = InstantModelsManager(
        tmp_path / "state",
        "https://example.invalid/api/v1/instant-models",
        client_factory=lambda _token: FakeClient(manifest, "https://example.invalid/file"),
    )
    manager.activate("im_live_" + "c" * 43)

    source = manager.source_for("image-generation")

    assert source is not None
    assert source.file_for("models/diffusion_models/model.safetensors") == file
    assert source.file_for("models/vae/missing.safetensors") is None


def test_instant_mode_rejects_incomplete_bundle_instead_of_mixing_sources(tmp_path) -> None:
    workflow = load_catalog(Path(__file__).parents[1] / "catalog" / "catalog.json")[0]
    source = InstantDownloadSource(
        bundle=InstantBundle(workflow.id, workflow.title, "", 0, ()),
        client=FakeClient(InstantManifest("v1", "2030-01-01T00:00:00Z", ()), "https://example.invalid/file"),
    )
    installer = Installer(tmp_path / "ComfyUI", StateStore(tmp_path / "state"), tmp_path / "restart.sh")

    with pytest.raises(InstallError, match="nie zawiera pliku pakietu"):
        installer._resolve_downloads(workflow, source)


def test_image_edit_instant_mode_downloads_gated_flux_from_r2(tmp_path) -> None:
    workflows = load_catalog(Path(__file__).parents[1] / "catalog" / "catalog.json")
    workflow = next(item for item in workflows if item.id == "image-edit")
    specs = (*workflow.downloads, *workflow.manual_files)
    remote_files = tuple(
        InstantFile(
            id=f"file-{index}",
            filename=Path(item.destination).name,
            size=item.size_bytes,
            sha256=item.sha256,
            destination=item.destination,
        )
        for index, item in enumerate(specs)
    )
    source = InstantDownloadSource(
        bundle=InstantBundle(workflow.id, workflow.title, "", sum(item.size for item in remote_files), remote_files),
        client=FakeClient(InstantManifest("v1", "2030-01-01T00:00:00Z", ()), "https://example.invalid/file"),
    )
    installer = Installer(tmp_path / "ComfyUI", StateStore(tmp_path / "state"), tmp_path / "restart.sh")

    downloads, pending_manual = installer._resolve_downloads(workflow, source)

    assert pending_manual == []
    assert all(item.instant for item in downloads)
    assert "models/unet/flux-2-klein-9b.safetensors" in {item.spec.destination for item in downloads}


def test_download_error_never_exposes_presigned_query(tmp_path, monkeypatch) -> None:
    secret_url = "https://example.invalid/model?X-Amz-Signature=do-not-log-me"

    def fail(*_args, **_kwargs):
        raise requests.ConnectionError(f"connection failed for {secret_url}")

    monkeypatch.setattr("launcher.download_engine.requests.get", fail)
    engine = DownloadEngine(tmp_path, attempts=1)
    request = DownloadRequest("Model", "model.bin", 10, "a" * 64, lambda: secret_url)

    try:
        engine.download(request, cancelled=lambda: False, progress=lambda *_args: None)
        assert False, "download should fail"
    except DownloadError as exc:
        assert "do-not-log-me" not in str(exc)
        assert "ConnectionError" in str(exc)


def test_parallel_range_download_uses_multiple_connections(tmp_path) -> None:
    payload = (b"parallel-model-data" * 500_000)[:8 * 1024 * 1024]
    observed_ranges: list[str] = []
    active = 0
    maximum_active = 0
    guard = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            nonlocal active, maximum_active
            range_header = self.headers.get("Range", "")
            start_text, end_text = range_header.removeprefix("bytes=").split("-")
            start = int(start_text)
            end = int(end_text)
            with guard:
                active += 1
                maximum_active = max(maximum_active, active)
                if range_header != "bytes=0-0":
                    observed_ranges.append(range_header)
            try:
                body = payload[start : end + 1]
                self.send_response(206)
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
                self.end_headers()
                for offset in range(0, len(body), 64 * 1024):
                    self.wfile.write(body[offset : offset + 64 * 1024])
                    time.sleep(0.002)
            finally:
                with guard:
                    active -= 1

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        engine = DownloadEngine(
            tmp_path,
            attempts=1,
            chunk_size=64 * 1024,
            parallelism=4,
            segment_size=1024 * 1024,
            parallel_threshold=2 * 1024 * 1024,
        )
        request = DownloadRequest(
            "Parallel model",
            "models/model.bin",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            lambda: f"http://127.0.0.1:{server.server_port}/model.bin",
        )

        engine.download(request, cancelled=lambda: False, progress=lambda *_args: None)

        assert (tmp_path / "models" / "model.bin").read_bytes() == payload
        assert maximum_active >= 2
        assert len(observed_ranges) == 8
        assert "bytes=0-1048575" in observed_ranges
        assert "bytes=7340032-8388607" in observed_ranges
        assert not (tmp_path / "models" / "model.bin.part.segments.json").exists()
    finally:
        server.shutdown()
        server.server_close()


def test_parallel_range_download_resumes_completed_segments(tmp_path) -> None:
    segment_size = 1024 * 1024
    payload = (b"resume-segment-data" * 300_000)[:4 * segment_size]
    observed_ranges: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            range_header = self.headers["Range"]
            start_text, end_text = range_header.removeprefix("bytes=").split("-")
            start, end = int(start_text), int(end_text)
            if range_header != "bytes=0-0":
                observed_ranges.append(range_header)
            body = payload[start : end + 1]
            self.send_response(206)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Content-Range", f"bytes {start}-{end}/{len(payload)}")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        target = tmp_path / "models" / "resume.bin"
        target.parent.mkdir(parents=True)
        part = target.with_name(target.name + ".part")
        part.write_bytes(payload[: 2 * segment_size] + b"\0" * (len(payload) - 2 * segment_size))
        metadata = part.with_name(part.name + ".segments.json")
        metadata.write_text(
            json.dumps({"version": 1, "size": len(payload), "segment_size": segment_size, "completed": [0, 1]}),
            encoding="utf-8",
        )
        engine = DownloadEngine(
            tmp_path,
            attempts=1,
            chunk_size=64 * 1024,
            parallelism=2,
            segment_size=segment_size,
            parallel_threshold=2 * segment_size,
        )
        request = DownloadRequest(
            "Resumed model",
            "models/resume.bin",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            lambda: f"http://127.0.0.1:{server.server_port}/resume.bin",
        )

        engine.download(request, cancelled=lambda: False, progress=lambda *_args: None)

        assert target.read_bytes() == payload
        assert set(observed_ranges) == {
            "bytes=2097152-3145727",
            "bytes=3145728-4194303",
        }
    finally:
        server.shutdown()
        server.server_close()
