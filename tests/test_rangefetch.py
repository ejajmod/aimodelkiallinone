import hashlib
import io
import json
from pathlib import Path

import pytest

from launcher import download_engine
from launcher.download_engine import DownloadEngine, DownloadError, DownloadRequest


MIB = 1024 * 1024
SECRET_URL = "https://account.r2.cloudflarestorage.com/models/model.bin?X-Amz-Signature=do-not-log-me"


def request_for(payload: bytes, url_provider=lambda: SECRET_URL, headers=None) -> DownloadRequest:
    return DownloadRequest(
        "Model",
        "models/model.bin",
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        url_provider,
        headers=headers or {},
    )


def engine_for(tmp_path: Path, **overrides) -> DownloadEngine:
    options = {
        "attempts": 3,
        # The engine never uses segments smaller than its chunk size.
        "chunk_size": 64 * 1024,
        "segment_size": MIB,
        "parallel_threshold": 2 * MIB,
        "rangefetch": "rangefetch",
        "rangefetch_connections": 128,
        "aria2c": "aria2c",
    }
    options.update(overrides)
    return DownloadEngine(tmp_path, **options)


class KeptInput(io.StringIO):
    def close(self) -> None:
        self.value = self.getvalue()


class FakeRangefetch:
    """Stands in for the rangefetch binary: writes the file and reports progress as JSON."""

    def __init__(self, payload: bytes, exit_codes: list[int]):
        self.payload = payload
        self.exit_codes = list(exit_codes)
        self.calls: list[tuple[list[str], KeptInput]] = []

    def __call__(self, command, **_kwargs):
        process = FakeProcess(self, command)
        self.calls.append((command, process.stdin))
        return process


class FakeProcess:
    def __init__(self, owner: FakeRangefetch, command: list[str]):
        self.returncode = owner.exit_codes.pop(0)
        out = Path(command[command.index("-out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        if self.returncode == 0:
            out.write_bytes(owner.payload)
        self.stdin = KeptInput()
        half = len(owner.payload) // 2
        self.stdout = io.StringIO(
            json.dumps({"downloaded": half, "speed": 600 * MIB}) + "\n"
            + json.dumps({"downloaded": len(owner.payload), "speed": 0}) + "\n"
        )
        self.stderr = io.StringIO("HTTP 403" if self.returncode == 3 else "")

    def poll(self):
        return self.returncode


def test_invocation_keeps_url_and_headers_out_of_argv(tmp_path) -> None:
    engine = engine_for(tmp_path, segment_size=64 * MIB)
    item = request_for(b"x" * 10, headers={"Authorization": "Bearer hidden-token"})

    command, stdin = engine.rangefetch_invocation(item, tmp_path / "model.bin.part", SECRET_URL)

    argv = " ".join(command)
    assert "do-not-log-me" not in argv and "hidden-token" not in argv
    assert command[command.index("-connections") + 1] == "128"
    assert command[command.index("-segment-mb") + 1] == "64"
    assert stdin.splitlines() == [SECRET_URL, "Authorization: Bearer hidden-token"]


def test_large_files_use_rangefetch_and_small_files_use_aria2(tmp_path) -> None:
    engine = engine_for(tmp_path)
    part = tmp_path / "model.bin.part"

    assert engine._use_rangefetch(request_for(b"x" * (2 * MIB)), part) is True
    assert engine._use_rangefetch(request_for(b"x" * MIB), part) is False
    assert engine._use_aria2(request_for(b"x" * MIB), part) is True


def test_download_started_by_aria2_is_finished_by_aria2(tmp_path) -> None:
    engine = engine_for(tmp_path)
    part = tmp_path / "model.bin.part"
    part.with_name(part.name + ".aria2").write_text("", encoding="utf-8")

    assert engine._use_rangefetch(request_for(b"x" * (4 * MIB)), part) is False


def test_rangefetch_download_is_verified_and_reports_progress(tmp_path, monkeypatch) -> None:
    payload = bytes(range(256)) * (3 * MIB // 256)
    fake = FakeRangefetch(payload, [0])
    monkeypatch.setattr(download_engine.subprocess, "Popen", fake)
    monkeypatch.setattr(download_engine.time, "sleep", lambda _seconds: None)
    engine = engine_for(tmp_path)
    reports: list[tuple[int, int, int]] = []

    size = engine.download(request_for(payload), cancelled=lambda: False, progress=lambda *args: reports.append(args))

    target = tmp_path / "models" / "model.bin"
    assert size == len(payload)
    assert target.read_bytes() == payload
    assert fake.calls[0][1].value.startswith(SECRET_URL)
    assert reports[-1] == (len(payload), len(payload), 0)
    assert engine.verified_marker(target).is_file()


def test_refused_url_is_renewed_on_the_next_attempt(tmp_path, monkeypatch) -> None:
    payload = b"model" * MIB
    fake = FakeRangefetch(payload, [3, 0])
    monkeypatch.setattr(download_engine.subprocess, "Popen", fake)
    monkeypatch.setattr(download_engine.time, "sleep", lambda _seconds: None)
    issued: list[str] = []

    def presign() -> str:
        issued.append(f"{SECRET_URL}&attempt={len(issued)}")
        return issued[-1]

    engine_for(tmp_path).download(request_for(payload, url_provider=presign), cancelled=lambda: False, progress=lambda *a: None)

    assert len(issued) == 2
    assert [call[1].value.splitlines()[0] for call in fake.calls] == issued


def test_rangefetch_failure_never_exposes_the_presigned_url(tmp_path, monkeypatch) -> None:
    payload = b"model" * MIB
    fake = FakeRangefetch(payload, [1])
    monkeypatch.setattr(download_engine.subprocess, "Popen", fake)

    class LeakyProcess(FakeProcess):
        def __init__(self, owner, command):
            super().__init__(owner, command)
            self.stderr = io.StringIO(f"Get {SECRET_URL}: connection reset")

    monkeypatch.setattr(download_engine.subprocess, "Popen", lambda command, **kwargs: LeakyProcess(fake, command))

    with pytest.raises(DownloadError) as error:
        engine_for(tmp_path, attempts=1).download(request_for(payload), cancelled=lambda: False, progress=lambda *a: None)

    assert "do-not-log-me" not in str(error.value)
