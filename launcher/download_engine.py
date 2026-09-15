from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import requests
from requests.adapters import HTTPAdapter


class DownloadCancelled(RuntimeError):
    pass


class DownloadError(RuntimeError):
    pass


URL_PATTERN = re.compile(r"https?://\S+")
MIB = 1024 * 1024
# aria2c exit status for "checksum validation failed".
ARIA2_CHECKSUM_FAILED = 32
# rangefetch exit status for HTTP 401/403: the presigned URL must be renewed.
RANGEFETCH_FORBIDDEN = 3


def redact_urls(text: str) -> str:
    """A presigned URL carries its signature in the query string, so none may reach a log."""
    return URL_PATTERN.sub("<url>", text)


@dataclass(frozen=True)
class DownloadRequest:
    name: str
    destination: str
    size_bytes: int | None
    sha256: str | None
    url_provider: Callable[[], str]
    headers: Mapping[str, str] = field(default_factory=dict)


class DownloadEngine:
    RETRYABLE_STATUS = {408, 425, 429, 500, 502, 503, 504}

    def __init__(
        self,
        root: Path,
        *,
        attempts: int = 5,
        chunk_size: int = 4 * 1024 * 1024,
        parallelism: int = 1,
        segment_size: int = 64 * 1024 * 1024,
        parallel_threshold: int = 256 * 1024 * 1024,
        aria2c: str | None = None,
        aria2_connections: int = 16,
        rangefetch: str | None = None,
        rangefetch_connections: int = 64,
    ):
        self.root = root.resolve()
        self.attempts = attempts
        self.chunk_size = chunk_size
        self.parallelism = max(1, min(int(parallelism), 256))
        self.segment_size = max(self.chunk_size, int(segment_size))
        self.parallel_threshold = max(self.segment_size, int(parallel_threshold))
        self.aria2c = aria2c
        self.aria2_connections = max(1, min(int(aria2_connections), 16))
        self.rangefetch = rangefetch
        self.rangefetch_connections = max(1, min(int(rangefetch_connections), 512))
        self._local = threading.local()

    def target(self, relative: str) -> Path:
        normalized = relative.replace("\\", "/")
        if not normalized or normalized.startswith("/") or any(part in {"", ".."} for part in normalized.split("/")):
            raise DownloadError(f"Niebezpieczna ścieżka docelowa: {relative}")
        target = (self.root / normalized).resolve()
        if target == self.root or self.root not in target.parents:
            raise DownloadError(f"Ścieżka wychodzi poza katalog modeli: {relative}")
        return target

    @staticmethod
    def sha256(path: Path, cancelled: Callable[[], bool] | None = None) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(8 * 1024 * 1024), b""):
                if cancelled and cancelled():
                    raise DownloadCancelled()
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def verified_marker(target: Path) -> Path:
        return target.with_name(f".{target.name}.aimodelki-verified")

    def _marker_matches(self, target: Path, expected_sha256: str) -> bool:
        try:
            stat = target.stat()
            recorded = json.loads(self.verified_marker(target).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return (
            isinstance(recorded, dict)
            and recorded.get("sha256") == expected_sha256.lower()
            and recorded.get("size") == stat.st_size
            and recorded.get("mtime_ns") == stat.st_mtime_ns
        )

    def _write_verified_marker(self, target: Path, sha256: str) -> None:
        try:
            stat = target.stat()
            self.verified_marker(target).write_text(
                json.dumps({"sha256": sha256.lower(), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}),
                encoding="utf-8",
            )
        except OSError:
            # The marker only saves a later re-hash; the file itself is already verified.
            pass

    def is_ready(
        self,
        target: Path,
        expected_size: int | None,
        expected_sha256: str | None,
        cancelled: Callable[[], bool] | None = None,
    ) -> bool:
        if not target.is_file():
            return False
        if expected_size and target.stat().st_size != expected_size:
            return False
        if not expected_sha256:
            return True
        # A file verified earlier and untouched since (same size and mtime) is not read again.
        if self._marker_matches(target, expected_sha256):
            return True
        if self.sha256(target, cancelled).lower() != expected_sha256.lower():
            return False
        self._write_verified_marker(target, expected_sha256)
        return True

    def download(
        self,
        item: DownloadRequest,
        *,
        cancelled: Callable[[], bool],
        progress: Callable[[int, int, int], None],
    ) -> int:
        target = self.target(item.destination)
        target.parent.mkdir(parents=True, exist_ok=True)
        if self.is_ready(target, item.size_bytes, item.sha256, cancelled):
            size = target.stat().st_size
            progress(size, item.size_bytes or size, 0)
            return size

        part = target.with_name(target.name + ".part")
        metadata = self._metadata_path(part)
        control = self._aria2_control_path(part)
        if part.exists() and item.size_bytes and part.stat().st_size > item.size_bytes:
            part.unlink()
            metadata.unlink(missing_ok=True)
            control.unlink(missing_ok=True)

        last_error: Exception | None = None
        for attempt in range(self.attempts):
            if cancelled():
                raise DownloadCancelled()
            try:
                verified = False
                if self._use_rangefetch(item, part):
                    self._transfer_rangefetch(item, part, cancelled, progress)
                elif self._use_aria2(item, part):
                    verified = self._transfer_aria2(item, part, cancelled, progress)
                elif (
                    self.parallelism > 1
                    and item.size_bytes
                    and item.size_bytes >= self.parallel_threshold
                ):
                    self._transfer_parallel(item, part, cancelled, progress)
                else:
                    metadata.unlink(missing_ok=True)
                    self._transfer(item, part, cancelled, progress)
                if item.size_bytes and part.stat().st_size != item.size_bytes:
                    raise DownloadError(
                        f"Niepełny plik {item.name}: {part.stat().st_size} z {item.size_bytes} bajtów"
                    )
                if item.sha256 and not verified and self.sha256(part, cancelled).lower() != item.sha256.lower():
                    part.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
                    control.unlink(missing_ok=True)
                    raise DownloadError(f"Suma SHA-256 nie zgadza się dla {item.name}")
                part.replace(target)
                metadata.unlink(missing_ok=True)
                control.unlink(missing_ok=True)
                if item.sha256:
                    self._write_verified_marker(target, item.sha256)
                return target.stat().st_size
            except DownloadCancelled:
                raise
            except (requests.RequestException, OSError, DownloadError) as exc:
                last_error = exc
                if isinstance(exc, DownloadError) and "SHA-256" in str(exc):
                    break
                if attempt + 1 < self.attempts:
                    time.sleep(min(2**attempt, 8))

        if isinstance(last_error, requests.HTTPError) and last_error.response is not None:
            reason = f"HTTP {last_error.response.status_code}"
        elif isinstance(last_error, requests.RequestException):
            # A requests exception may contain the full presigned URL and query signature.
            reason = type(last_error).__name__
        else:
            reason = redact_urls(str(last_error))
        raise DownloadError(f"Nie udało się pobrać {item.name}: {reason}")

    @staticmethod
    def _metadata_path(part: Path) -> Path:
        return part.with_name(part.name + ".segments.json")

    @staticmethod
    def _aria2_control_path(part: Path) -> Path:
        return part.with_name(part.name + ".aria2")

    def _use_rangefetch(self, item: DownloadRequest, part: Path) -> bool:
        # Large files only; a download aria2c started keeps its own control file and
        # only aria2c can finish it.
        return bool(
            self.rangefetch
            and item.size_bytes
            and item.size_bytes >= self.parallel_threshold
            and not self._aria2_control_path(part).exists()
        )

    def _use_aria2(self, item: DownloadRequest, part: Path) -> bool:
        # Segments written by the Python engine or rangefetch are finished by those engines.
        return bool(self.aria2c and item.size_bytes and not self._metadata_path(part).exists())

    @staticmethod
    def _header_lines(item: DownloadRequest, prefix: str) -> list[str]:
        lines = []
        for key, value in item.headers.items():
            if not value:
                continue
            if any(character in f"{key}{value}" for character in "\r\n"):
                raise DownloadError(f"Nieprawidłowy nagłówek żądania dla {item.name}")
            lines.append(f"{prefix}{key}: {value}")
        return lines

    def rangefetch_invocation(self, item: DownloadRequest, part: Path, url: str) -> tuple[list[str], str]:
        """Build the rangefetch command line and its stdin (URL and headers, never argv)."""
        if not self.rangefetch:
            raise DownloadError("rangefetch nie jest dostępny")
        command = [
            self.rangefetch,
            "-out",
            str(part),
            "-size",
            str(int(item.size_bytes or 0)),
            "-connections",
            str(self.rangefetch_connections),
            # Same segment layout as the Python engine, so each can resume the other.
            "-segment-mb",
            str(max(1, self.segment_size // MIB)),
        ]
        return command, "\n".join([url, *self._header_lines(item, "")]) + "\n"

    def _transfer_rangefetch(
        self,
        item: DownloadRequest,
        part: Path,
        cancelled: Callable[[], bool],
        progress: Callable[[int, int, int], None],
    ) -> None:
        expected = int(item.size_bytes or 0)
        command, input_file = self.rangefetch_invocation(item, part, item.url_provider())
        part.parent.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        latest = {"downloaded": 0, "speed": 0}
        errors: list[str] = []

        def read_progress() -> None:
            for line in process.stdout:
                try:
                    state = json.loads(line)
                    latest["downloaded"] = int(state.get("downloaded", 0))
                    latest["speed"] = int(state.get("speed", 0))
                except (ValueError, TypeError, AttributeError):
                    continue

        readers = [
            threading.Thread(target=read_progress, daemon=True),
            threading.Thread(target=lambda: errors.append(process.stderr.read()), daemon=True),
        ]
        for reader in readers:
            reader.start()
        try:
            process.stdin.write(input_file)
            process.stdin.close()
        except OSError:
            # rangefetch exited before reading its input; the exit status explains why.
            pass

        while process.poll() is None:
            if cancelled():
                # Completed segments are saved on SIGTERM, so the next start resumes them.
                self._stop_process(process)
                raise DownloadCancelled()
            time.sleep(0.5)
            progress(min(latest["downloaded"], expected), expected, latest["speed"])
        for reader in readers:
            reader.join(timeout=5)

        detail = redact_urls("".join(errors).strip())[-300:]
        if process.returncode == RANGEFETCH_FORBIDDEN:
            # The next attempt asks for a fresh presigned URL.
            raise DownloadError(f"Serwer odrzucił adres pobierania ({detail or 'HTTP 403'})")
        if process.returncode:
            raise DownloadError(f"rangefetch zakończył pracę z kodem {process.returncode}. {detail}".strip())
        progress(expected, expected, 0)

    def aria2_invocation(self, item: DownloadRequest, part: Path, url: str) -> tuple[list[str], str]:
        """Build the aria2c command line and its input file.

        The URL and the request headers travel through stdin, never argv, so a presigned
        signature cannot show up in the process list. The target directory and file name
        belong in the input file too: aria2c ignores a global --out for input-file entries
        and would name the file after the redirect target instead.
        """
        if not self.aria2c:
            raise DownloadError("aria2c nie jest dostępny")
        connections = str(self.aria2_connections)
        command = [
            self.aria2c,
            "--no-conf=true",
            "--input-file=-",
            f"--max-connection-per-server={connections}",
            f"--split={connections}",
            # Small pieces let idle connections take over the tail of a slow one.
            "--min-split-size=4M",
            "--continue=true",
            "--allow-overwrite=true",
            "--auto-file-renaming=false",
            # Pre-allocation would make the progress reading jump to 100% at once.
            "--file-allocation=none",
            "--disk-cache=64M",
            "--max-tries=5",
            "--retry-wait=3",
            "--connect-timeout=20",
            "--timeout=60",
            "--auto-save-interval=10",
            "--summary-interval=0",
            "--console-log-level=warn",
            "--download-result=hide",
            "--show-console-readout=false",
        ]
        if item.sha256:
            command.append(f"--checksum=sha-256={item.sha256.lower()}")
        lines = [url, f"  dir={part.parent}", f"  out={part.name}", *self._header_lines(item, "  header=")]
        return command, "\n".join(lines) + "\n"

    @staticmethod
    def written_bytes(path: Path, cap: int = 0) -> int:
        """Bytes actually stored in a sparse file that is written at many offsets at once."""
        try:
            stat = path.stat()
        except OSError:
            return 0
        blocks = getattr(stat, "st_blocks", None)
        written = blocks * 512 if blocks is not None else stat.st_size
        return min(written, cap) if cap else written

    @staticmethod
    def _stop_process(process: subprocess.Popen) -> None:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()

    def _transfer_aria2(
        self,
        item: DownloadRequest,
        part: Path,
        cancelled: Callable[[], bool],
        progress: Callable[[int, int, int], None],
    ) -> bool:
        """Download with aria2c. Returns True when aria2c has verified the SHA-256 itself."""
        expected = int(item.size_bytes or 0)
        command, input_file = self.aria2_invocation(item, part, item.url_provider())
        part.parent.mkdir(parents=True, exist_ok=True)
        started_bytes = self.written_bytes(part, expected)
        progress(started_bytes, expected, 0)

        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        output: list[str] = []
        reader = threading.Thread(target=lambda: output.append(process.stdout.read()), daemon=True)
        reader.start()
        try:
            process.stdin.write(input_file)
            process.stdin.close()
        except OSError:
            # aria2c exited before reading its input; the exit status below explains why.
            pass

        highest = started_bytes
        window_started = time.monotonic()
        window_bytes = started_bytes
        speed = 0
        while process.poll() is None:
            if cancelled():
                self._stop_process(process)
                raise DownloadCancelled()
            time.sleep(0.5)
            # ext4 delays allocation, so the reading can briefly go down; never show that.
            highest = max(highest, self.written_bytes(part, expected))
            now = time.monotonic()
            if now - window_started >= 1.0:
                sample = int((highest - window_bytes) / (now - window_started))
                speed = sample if not speed else int(speed * 0.6 + sample * 0.4)
                window_started, window_bytes = now, highest
            progress(highest, expected, speed)
        reader.join(timeout=5)

        if process.returncode == ARIA2_CHECKSUM_FAILED:
            part.unlink(missing_ok=True)
            self._aria2_control_path(part).unlink(missing_ok=True)
            raise DownloadError(f"Suma SHA-256 nie zgadza się dla {item.name}")
        if process.returncode:
            detail = redact_urls("".join(output).strip())[-300:]
            raise DownloadError(f"aria2c zakończył pracę z kodem {process.returncode}. {detail}".strip())
        progress(expected, expected, 0)
        return bool(item.sha256)

    def partial_size(self, target: Path, expected_size: int) -> int:
        part = target.with_name(target.name + ".part")
        if not part.is_file():
            return 0
        metadata = self._metadata_path(part)
        if not metadata.is_file():
            return min(part.stat().st_size, expected_size)
        try:
            state = json.loads(metadata.read_text(encoding="utf-8"))
            if state.get("size") != expected_size or state.get("segment_size") != self.segment_size:
                return 0
            completed = {int(value) for value in state.get("completed", [])}
            return sum(
                min(self.segment_size, expected_size - index * self.segment_size)
                for index in completed
                if 0 <= index * self.segment_size < expected_size
            )
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            return 0

    def _session(self) -> requests.Session:
        """One keep-alive session per worker thread, so segments reuse TCP and TLS."""
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            adapter = HTTPAdapter(pool_connections=1, pool_maxsize=2, max_retries=0)
            session.mount("https://", adapter)
            session.mount("http://", adapter)
            self._local.session = session
        return session

    def _transfer_parallel(
        self,
        item: DownloadRequest,
        part: Path,
        cancelled: Callable[[], bool],
        progress: Callable[[int, int, int], None],
    ) -> None:
        expected = int(item.size_bytes or 0)
        if expected <= 0:
            self._transfer(item, part, cancelled, progress)
            return

        headers = {key: value for key, value in item.headers.items() if value}
        url = item.url_provider()
        if not self._supports_ranges(url, headers, expected):
            metadata = self._metadata_path(part)
            if metadata.exists():
                part.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
            self._transfer(item, part, cancelled, progress)
            return

        metadata = self._metadata_path(part)
        segment_count = (expected + self.segment_size - 1) // self.segment_size
        completed: set[int] = set()
        legacy_prefix = 0
        if metadata.is_file():
            try:
                state = json.loads(metadata.read_text(encoding="utf-8"))
                if state.get("size") == expected and state.get("segment_size") == self.segment_size:
                    completed = {
                        int(index)
                        for index in state.get("completed", [])
                        if 0 <= int(index) < segment_count
                    }
                else:
                    part.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
            except (OSError, ValueError, TypeError, json.JSONDecodeError):
                part.unlink(missing_ok=True)
                metadata.unlink(missing_ok=True)
        elif part.is_file():
            legacy_prefix = min(part.stat().st_size, expected)
            completed = {
                index
                for index in range(segment_count)
                if min((index + 1) * self.segment_size, expected) <= legacy_prefix
            }

        part.parent.mkdir(parents=True, exist_ok=True)
        with part.open("r+b" if part.exists() else "w+b") as handle:
            handle.truncate(expected)
        self._write_metadata(metadata, expected, completed)

        completed_bytes = sum(
            min(self.segment_size, expected - index * self.segment_size) for index in completed
        )
        progress(completed_bytes, expected, 0)
        missing = [index for index in range(segment_count) if index not in completed]
        if not missing:
            with part.open("r+b") as handle:
                os.fsync(handle.fileno())
            return

        guard = threading.Lock()
        abort = threading.Event()
        transferred = completed_bytes
        last_report_bytes = transferred
        last_report_time = time.monotonic()
        smoothed_speed = 0

        def report(amount: int) -> None:
            nonlocal transferred, last_report_bytes, last_report_time, smoothed_speed
            with guard:
                transferred += amount
                now = time.monotonic()
                elapsed = now - last_report_time
                if elapsed >= 0.4:
                    sample = int((transferred - last_report_bytes) / max(0.001, elapsed))
                    smoothed_speed = sample if not smoothed_speed else int(smoothed_speed * 0.75 + sample * 0.25)
                    progress(min(transferred, expected), expected, smoothed_speed)
                    last_report_bytes = transferred
                    last_report_time = now

        def fetch(index: int) -> int:
            if cancelled():
                raise DownloadCancelled()
            if abort.is_set():
                return 0
            start = index * self.segment_size
            end = min(expected - 1, start + self.segment_size - 1)
            request_headers = dict(headers)
            request_headers["Range"] = f"bytes={start}-{end}"
            with self._session().get(url, headers=request_headers, stream=True, timeout=(20, 120)) as response:
                if response.status_code in self.RETRYABLE_STATUS or response.status_code in {401, 403}:
                    raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
                response.raise_for_status()
                content_range = response.headers.get("Content-Range", "")
                if response.status_code != 206 or not content_range.startswith(f"bytes {start}-{end}/"):
                    raise DownloadError("Serwer zwrócił nieprawidłową odpowiedź HTTP Range")
                offset = start
                with part.open("r+b", buffering=0) as handle:
                    handle.seek(start)
                    for chunk in response.iter_content(chunk_size=self.chunk_size):
                        if cancelled():
                            raise DownloadCancelled()
                        if abort.is_set():
                            return 0
                        if not chunk:
                            continue
                        if offset + len(chunk) > end + 1:
                            raise DownloadError("Serwer przesłał więcej danych niż żądany zakres")
                        handle.write(chunk)
                        offset += len(chunk)
                        report(len(chunk))
                    handle.flush()
                if offset != end + 1:
                    raise DownloadError(
                        f"Niepełny segment {item.name}: {offset - start} z {end - start + 1} bajtów"
                    )
            with guard:
                completed.add(index)
                self._write_metadata(metadata, expected, completed)
            return end - start + 1

        with ThreadPoolExecutor(max_workers=min(self.parallelism, len(missing))) as executor:
            futures = [executor.submit(fetch, index) for index in missing]
            try:
                for future in as_completed(futures):
                    future.result()
            except Exception:
                abort.set()
                for future in futures:
                    future.cancel()
                raise

        with part.open("r+b") as handle:
            os.fsync(handle.fileno())
        progress(expected, expected, 0)

    def _supports_ranges(self, url: str, headers: Mapping[str, str], expected: int) -> bool:
        probe_headers = dict(headers)
        probe_headers["Range"] = "bytes=0-0"
        with requests.get(url, headers=probe_headers, stream=True, timeout=(20, 30)) as response:
            if response.status_code in self.RETRYABLE_STATUS or response.status_code in {401, 403}:
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            return response.status_code == 206 and response.headers.get("Content-Range", "") == f"bytes 0-0/{expected}"

    def _write_metadata(self, path: Path, size: int, completed: set[int]) -> None:
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(
                {"version": 1, "size": size, "segment_size": self.segment_size, "completed": sorted(completed)},
                separators=(",", ":"),
            ),
            encoding="utf-8",
        )
        temporary.replace(path)

    def _transfer(
        self,
        item: DownloadRequest,
        part: Path,
        cancelled: Callable[[], bool],
        progress: Callable[[int, int, int], None],
    ) -> None:
        resume_at = part.stat().st_size if part.exists() else 0
        headers = {key: value for key, value in item.headers.items() if value}
        if resume_at:
            headers["Range"] = f"bytes={resume_at}-"

        url = item.url_provider()
        with requests.get(url, headers=headers, stream=True, timeout=(20, 120)) as response:
            if response.status_code == 416 and item.size_bytes and resume_at == item.size_bytes:
                progress(resume_at, item.size_bytes, 0)
                return
            if response.status_code in self.RETRYABLE_STATUS or response.status_code in {401, 403}:
                raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
            response.raise_for_status()
            append = resume_at > 0 and response.status_code == 206
            if not append:
                resume_at = 0
            response_bytes = int(response.headers.get("Content-Length", "0") or 0)
            expected = item.size_bytes or (resume_at + response_bytes)
            downloaded = resume_at
            window_started = time.monotonic()
            window_bytes = 0
            with part.open("ab" if append else "wb") as handle:
                for chunk in response.iter_content(chunk_size=self.chunk_size):
                    if cancelled():
                        raise DownloadCancelled()
                    if not chunk:
                        continue
                    handle.write(chunk)
                    downloaded += len(chunk)
                    window_bytes += len(chunk)
                    now = time.monotonic()
                    if now - window_started >= 0.4:
                        progress(downloaded, expected, int(window_bytes / max(0.001, now - window_started)))
                        window_started = now
                        window_bytes = 0
                handle.flush()
                os.fsync(handle.fileno())
            progress(downloaded, expected, 0)
