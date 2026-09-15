import hashlib
import io
import os
from pathlib import Path

import pytest

from launcher import download_engine
from launcher.download_engine import DownloadEngine, DownloadError, DownloadRequest, redact_urls


SECRET_URL = "https://bucket.r2.cloudflarestorage.com/models/model.bin?X-Amz-Signature=do-not-log-me"


def request_for(payload: bytes, headers=None) -> DownloadRequest:
    return DownloadRequest(
        "Model",
        "models/model.bin",
        len(payload),
        hashlib.sha256(payload).hexdigest(),
        lambda: SECRET_URL,
        headers=headers or {},
    )


def test_invocation_keeps_url_and_headers_out_of_argv(tmp_path) -> None:
    engine = DownloadEngine(tmp_path, aria2c="aria2c", aria2_connections=16)
    item = request_for(b"weights", headers={"Authorization": "Bearer hidden-token"})

    part = tmp_path / "models" / "model.bin.part"
    command, input_file = engine.aria2_invocation(item, part, SECRET_URL)

    argv = " ".join(command)
    assert "do-not-log-me" not in argv
    assert "hidden-token" not in argv
    assert "--input-file=-" in command
    assert "--split=16" in command
    assert f"--checksum=sha-256={item.sha256}" in command
    # aria2c ignores a global --out for input-file entries, so the name must be per URI.
    assert not any(argument.startswith(("--out", "--dir")) for argument in command)
    assert input_file.splitlines() == [
        SECRET_URL,
        f"  dir={part.parent}",
        "  out=model.bin.part",
        "  header=Authorization: Bearer hidden-token",
    ]


def test_header_with_line_break_is_rejected(tmp_path) -> None:
    engine = DownloadEngine(tmp_path, aria2c="aria2c")

    with pytest.raises(DownloadError):
        engine.aria2_invocation(request_for(b"x", headers={"X-Test": "a\nurl=https://evil"}), tmp_path / "x.part", SECRET_URL)


def test_urls_are_redacted_from_messages() -> None:
    assert redact_urls(f"errorCode=22 URI={SECRET_URL} failed") == "errorCode=22 URI=<url> failed"


class KeptInput(io.StringIO):
    """stdin of the fake process: writes the download once the input file is complete."""

    def __init__(self, payload: bytes):
        super().__init__()
        self.payload = payload

    def close(self) -> None:
        self.value = self.getvalue()
        options = dict(
            line.strip().split("=", 1) for line in self.value.splitlines()[1:] if "=" in line
        )
        Path(options["dir"], options["out"]).write_bytes(self.payload)


class FakeAria2:
    """Stands in for aria2c: reads the input file like aria2c does and exits with a fixed status."""

    def __init__(self, payload: bytes, returncode: int):
        self.payload = payload
        self.code = returncode

    def __call__(self, command, **_kwargs):
        self.command = command
        self.stdin = KeptInput(self.payload)
        self.stdout = io.StringIO(f"[ERROR] URI={SECRET_URL}")
        self.returncode = self.code
        return self

    def poll(self):
        return self.returncode


def test_aria2_download_is_trusted_without_a_second_hash(tmp_path, monkeypatch) -> None:
    payload = b"model-weights" * 1000
    fake = FakeAria2(payload, 0)
    monkeypatch.setattr(download_engine.subprocess, "Popen", fake)
    monkeypatch.setattr(
        DownloadEngine, "sha256", staticmethod(lambda *_args, **_kwargs: pytest.fail("aria2c verified the file"))
    )
    engine = DownloadEngine(tmp_path, attempts=1, aria2c="aria2c")

    size = engine.download(request_for(payload), cancelled=lambda: False, progress=lambda *_args: None)

    assert size == len(payload)
    assert (tmp_path / "models" / "model.bin").read_bytes() == payload
    assert fake.stdin.value.startswith(SECRET_URL)
    assert engine.verified_marker(tmp_path / "models" / "model.bin").is_file()


def test_aria2_checksum_failure_discards_the_part_file(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(download_engine.subprocess, "Popen", FakeAria2(b"corrupt", 32))
    engine = DownloadEngine(tmp_path, attempts=3, aria2c="aria2c")

    with pytest.raises(DownloadError, match="SHA-256"):
        engine.download(request_for(b"expected"), cancelled=lambda: False, progress=lambda *_args: None)

    assert not (tmp_path / "models" / "model.bin.part").exists()


def test_aria2_error_never_exposes_the_presigned_url(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(download_engine.subprocess, "Popen", FakeAria2(b"partial", 22))
    engine = DownloadEngine(tmp_path, attempts=1, aria2c="aria2c")

    with pytest.raises(DownloadError) as error:
        engine.download(request_for(b"expected-data"), cancelled=lambda: False, progress=lambda *_args: None)

    assert "do-not-log-me" not in str(error.value)
    assert "22" in str(error.value)


def test_segments_from_the_python_engine_are_finished_by_it(tmp_path) -> None:
    engine = DownloadEngine(tmp_path, aria2c="aria2c")
    part = tmp_path / "model.bin.part"
    metadata = part.with_name(part.name + ".segments.json")
    metadata.write_text("{}", encoding="utf-8")

    assert engine._use_aria2(request_for(b"x"), part) is False
    metadata.unlink()
    assert engine._use_aria2(request_for(b"x"), part) is True


def test_verified_file_is_not_hashed_again_until_it_changes(tmp_path, monkeypatch) -> None:
    payload = b"weights" * 1000
    target = tmp_path / "model.bin"
    target.write_bytes(payload)
    digest = hashlib.sha256(payload).hexdigest()
    calls: list[Path] = []
    original = DownloadEngine.sha256
    monkeypatch.setattr(
        DownloadEngine,
        "sha256",
        staticmethod(lambda path, cancelled=None: calls.append(path) or original(path, cancelled)),
    )
    engine = DownloadEngine(tmp_path)

    assert engine.is_ready(target, len(payload), digest)
    assert engine.is_ready(target, len(payload), digest)
    assert len(calls) == 1

    target.write_bytes(b"x" * len(payload))
    stat = target.stat()
    os.utime(target, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    assert not engine.is_ready(target, len(payload), digest)
    assert len(calls) == 2
