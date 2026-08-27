import hashlib
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from launcher.catalog import DownloadSpec
from launcher.installer import Installer
from launcher.state import StateStore


def test_comfy_python_prefers_cuda_13_environment(monkeypatch, tmp_path) -> None:
    monkeypatch.delenv("COMFYUI_PYTHON", raising=False)
    comfy_root = tmp_path / "ComfyUI"
    cu128 = comfy_root / ".venv-cu128" / "bin" / "python"
    cu130 = comfy_root / ".venv-cu130" / "bin" / "python"
    cu128.parent.mkdir(parents=True)
    cu130.parent.mkdir(parents=True)
    cu128.touch()
    cu130.touch()

    installer = Installer(comfy_root, StateStore(tmp_path / "state"), tmp_path / "restart.sh")

    assert installer._comfy_python() == cu130


def test_comfy_python_honors_explicit_override(monkeypatch, tmp_path) -> None:
    configured = tmp_path / "custom-python"
    monkeypatch.setenv("COMFYUI_PYTHON", str(configured))
    installer = Installer(tmp_path / "ComfyUI", StateStore(tmp_path / "state"), tmp_path / "restart.sh")

    assert installer._comfy_python() == configured


def test_part_download_resumes_with_http_range(tmp_path) -> None:
    payload = (b"model-weights-" * 4096) + b"done"
    observed_ranges = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            range_header = self.headers.get("Range")
            observed_ranges.append(range_header)
            start = int(range_header.removeprefix("bytes=").removesuffix("-")) if range_header else 0
            body = payload[start:]
            self.send_response(206 if range_header else 200)
            self.send_header("Content-Length", str(len(body)))
            if range_header:
                self.send_header("Content-Range", f"bytes {start}-{len(payload) - 1}/{len(payload)}")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args):
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        comfy_root = tmp_path / "ComfyUI"
        target = comfy_root / "models" / "checkpoints" / "model.bin"
        target.parent.mkdir(parents=True)
        part = target.with_name(target.name + ".part")
        part.write_bytes(payload[:137])

        installer = Installer(comfy_root, StateStore(tmp_path / "state"), tmp_path / "restart.sh")
        spec = DownloadSpec(
            name="Test model",
            url=f"http://127.0.0.1:{server.server_port}/model.bin",
            destination="models/checkpoints/model.bin",
            size_bytes=len(payload),
            sha256=hashlib.sha256(payload).hexdigest(),
        )

        installer._download(spec, 0, len(payload), 1, 1)

        assert observed_ranges == ["bytes=137-"]
        assert target.read_bytes() == payload
        assert not part.exists()
    finally:
        server.shutdown()
        server.server_close()
