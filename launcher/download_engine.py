from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping

import requests


class DownloadCancelled(RuntimeError):
    pass


class DownloadError(RuntimeError):
    pass


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
    ):
        self.root = root.resolve()
        self.attempts = attempts
        self.chunk_size = chunk_size
        self.parallelism = max(1, min(int(parallelism), 64))
        self.segment_size = max(self.chunk_size, int(segment_size))
        self.parallel_threshold = max(self.segment_size, int(parallel_threshold))

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
        return not expected_sha256 or self.sha256(target, cancelled).lower() == expected_sha256.lower()

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
        if part.exists() and item.size_bytes and part.stat().st_size > item.size_bytes:
            part.unlink()
            metadata.unlink(missing_ok=True)

        last_error: Exception | None = None
        for attempt in range(self.attempts):
            if cancelled():
                raise DownloadCancelled()
            try:
                if (
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
                if item.sha256 and self.sha256(part, cancelled).lower() != item.sha256.lower():
                    part.unlink(missing_ok=True)
                    metadata.unlink(missing_ok=True)
                    raise DownloadError(f"Suma SHA-256 nie zgadza się dla {item.name}")
                part.replace(target)
                metadata.unlink(missing_ok=True)
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
            reason = str(last_error)
        raise DownloadError(f"Nie udało się pobrać {item.name}: {reason}")

    @staticmethod
    def _metadata_path(part: Path) -> Path:
        return part.with_name(part.name + ".segments.json")

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
            with requests.get(url, headers=request_headers, stream=True, timeout=(20, 120)) as response:
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
