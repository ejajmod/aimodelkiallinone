from __future__ import annotations

import hashlib
import os
import shutil
import signal
import subprocess
import tarfile
import threading
import time
import zipfile
from pathlib import Path
from typing import Callable

import requests

from .catalog import DownloadSpec, NodeSpec, WorkflowSpec
from .state import StateStore


class InstallCancelled(RuntimeError):
    pass


class InstallError(RuntimeError):
    pass


class Installer:
    ACTIVE_STATES = {"queued", "downloading", "installing", "restarting"}

    def __init__(self, comfyui_root: Path, store: StateStore, restart_script: Path):
        self.comfyui_root = comfyui_root.resolve()
        self.store = store
        self.restart_script = restart_script
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._guard = threading.Lock()

    @property
    def active(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def _comfy_python(self) -> Path:
        configured = os.getenv("COMFYUI_PYTHON", "").strip()
        if configured:
            return Path(configured)
        candidates = (
            self.comfyui_root / ".venv-cu130" / "bin" / "python",
            self.comfyui_root / ".venv-cu128" / "bin" / "python",
            self.comfyui_root / ".venv" / "bin" / "python",
        )
        return next((candidate for candidate in candidates if candidate.is_file()), candidates[0])

    def start(self, workflow: WorkflowSpec) -> None:
        with self._guard:
            if self.active:
                raise InstallError("Inny instalator jest już uruchomiony")
            if not workflow.configured:
                missing = ", ".join(workflow.missing_env) or "downloads/custom_nodes"
                raise InstallError(f"Instalator wymaga konfiguracji: {missing}")
            comfy_python = self._comfy_python()
            if not (self.comfyui_root / "main.py").is_file() or not comfy_python.is_file():
                raise InstallError("ComfyUI jest jeszcze inicjalizowane. Poczekaj chwilę i spróbuj ponownie.")
            self._cancel.clear()
            self.store.update(
                status="queued",
                workflow_id=workflow.id,
                workflow_title=workflow.title,
                progress=0,
                current_item=None,
                current_index=0,
                total_items=len(workflow.downloads) + len(workflow.custom_nodes),
                downloaded_bytes=0,
                total_bytes=sum(item.size_bytes or 0 for item in workflow.downloads),
                speed_bytes_per_second=0,
                error=None,
                message="Przygotowuję instalację…",
            )
            self._thread = threading.Thread(target=self._run, args=(workflow,), daemon=True)
            self._thread.start()

    def cancel(self) -> None:
        if not self.active:
            raise InstallError("Brak aktywnej instalacji")
        self._cancel.set()
        self.store.update(message="Zatrzymywanie po zapisaniu bieżącego fragmentu…")

    def _check_cancelled(self) -> None:
        if self._cancel.is_set():
            raise InstallCancelled()

    def _target(self, relative: str) -> Path:
        target = (self.comfyui_root / relative).resolve()
        if target != self.comfyui_root and self.comfyui_root not in target.parents:
            raise InstallError(f"Ścieżka wychodzi poza COMFYUI_ROOT: {relative}")
        return target

    def _run(self, workflow: WorkflowSpec) -> None:
        try:
            self.comfyui_root.mkdir(parents=True, exist_ok=True)
            total_items = len(workflow.downloads) + len(workflow.custom_nodes)
            total_expected = sum(item.size_bytes or 0 for item in workflow.downloads)
            completed_expected = 0

            for index, item in enumerate(workflow.downloads, start=1):
                self._check_cancelled()
                self.store.update(
                    status="downloading",
                    current_item=item.name,
                    current_index=index,
                    total_items=total_items,
                    message=f"Pobieranie: {item.name}",
                )
                actual_size = self._download(item, completed_expected, total_expected, index, total_items)
                completed_expected += item.size_bytes or actual_size

            node_base = len(workflow.downloads)
            for offset, node in enumerate(workflow.custom_nodes, start=1):
                self._check_cancelled()
                index = node_base + offset
                progress = 90 + round((offset - 1) / max(1, len(workflow.custom_nodes)) * 9)
                self.store.update(
                    status="installing",
                    current_item=node.name,
                    current_index=index,
                    total_items=total_items,
                    progress=progress,
                    speed_bytes_per_second=0,
                    message=f"Instalowanie węzła: {node.name}",
                )
                self._install_node(node)

            self._check_cancelled()
            self.store.update(status="restarting", progress=99, message="Restartuję ComfyUI…")
            self._restart_comfyui()
            installed = set(self.store.get().get("installed_workflows", []))
            installed.add(workflow.id)
            self.store.update(
                status="complete",
                progress=100,
                speed_bytes_per_second=0,
                installed_workflows=sorted(installed),
                message="Pakiet jest gotowy. ComfyUI zostało uruchomione ponownie.",
            )
        except InstallCancelled:
            self.store.update(
                status="cancelled",
                speed_bytes_per_second=0,
                message="Instalacja zatrzymana. Częściowe pliki zachowano do wznowienia.",
            )
        except Exception as exc:  # status must survive every worker failure
            self.store.update(
                status="failed",
                speed_bytes_per_second=0,
                error=str(exc),
                message="Instalacja nie powiodła się. Możesz spróbować ponownie.",
            )

    def _download(
        self,
        item: DownloadSpec,
        completed_before: int,
        total_expected: int,
        item_index: int,
        total_items: int,
    ) -> int:
        target = self._target(item.destination)
        target.parent.mkdir(parents=True, exist_ok=True)

        if target.exists() and (not item.sha256 or self._sha256(target) == item.sha256):
            size = target.stat().st_size
            self._publish_download_progress(
                item,
                completed_before + (item.size_bytes or size),
                total_expected,
                size,
                0,
                item_index,
                total_items,
            )
            return size

        part = target.with_name(target.name + ".part")
        resume_at = part.stat().st_size if part.exists() else 0
        headers = {key: value for key, value in item.headers.items() if value}
        if resume_at:
            headers["Range"] = f"bytes={resume_at}-"

        with requests.get(item.url, headers=headers, stream=True, timeout=(20, 120)) as response:
            if response.status_code == 416 and item.size_bytes and resume_at == item.size_bytes:
                response.close()
            else:
                response.raise_for_status()
                append = resume_at > 0 and response.status_code == 206
                if not append:
                    resume_at = 0
                response_total = int(response.headers.get("Content-Length", "0") or 0)
                inferred_size = resume_at + response_total if response_total else item.size_bytes or 0
                started = time.monotonic()
                window_started = started
                window_bytes = 0
                downloaded = resume_at
                with part.open("ab" if append else "wb") as handle:
                    for chunk in response.iter_content(chunk_size=4 * 1024 * 1024):
                        self._check_cancelled()
                        if not chunk:
                            continue
                        handle.write(chunk)
                        downloaded += len(chunk)
                        window_bytes += len(chunk)
                        now = time.monotonic()
                        if now - window_started >= 0.4:
                            speed = int(window_bytes / max(0.001, now - window_started))
                            expected = item.size_bytes or inferred_size
                            aggregate_total = total_expected or expected
                            aggregate_done = completed_before + min(downloaded, expected or downloaded)
                            self._publish_download_progress(
                                item,
                                aggregate_done,
                                aggregate_total,
                                downloaded,
                                speed,
                                item_index,
                                total_items,
                            )
                            window_started = now
                            window_bytes = 0
                    handle.flush()
                    os.fsync(handle.fileno())

        if item.sha256 and self._sha256(part) != item.sha256:
            part.unlink(missing_ok=True)
            raise InstallError(f"Suma SHA-256 nie zgadza się dla {item.name}")
        part.replace(target)

        if item.extract:
            self.store.update(message=f"Rozpakowywanie: {item.name}")
            self._extract(target, target.parent, item.extract)

        size = target.stat().st_size
        self._publish_download_progress(
            item,
            completed_before + (item.size_bytes or size),
            total_expected or (item.size_bytes or size),
            size,
            0,
            item_index,
            total_items,
        )
        return size

    def _publish_download_progress(
        self,
        item: DownloadSpec,
        aggregate_done: int,
        aggregate_total: int,
        current_done: int,
        speed: int,
        item_index: int,
        total_items: int,
    ) -> None:
        if aggregate_total:
            progress = min(90, round(aggregate_done / aggregate_total * 90))
        else:
            progress = round((item_index - 1) / max(total_items, 1) * 90)
        self.store.update(
            progress=progress,
            downloaded_bytes=aggregate_done,
            total_bytes=aggregate_total,
            current_downloaded_bytes=current_done,
            current_total_bytes=item.size_bytes,
            speed_bytes_per_second=speed,
        )

    def _install_node(self, node: NodeSpec) -> None:
        destination = self._target(node.directory)
        if destination.exists() and not (destination / ".git").exists():
            raise InstallError(f"Katalog {node.directory} istnieje, ale nie jest repozytorium git")
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            self._run_command(["git", "clone", "--filter=blob:none", node.repository, str(destination)])
        self._run_command(["git", "-C", str(destination), "fetch", "--depth", "1", "origin", node.revision])
        self._run_command(["git", "-C", str(destination), "checkout", "--detach", "FETCH_HEAD"])

        python = str(self._comfy_python())
        requirements = destination / "requirements.txt"
        if node.install_requirements and requirements.exists():
            command = [python, "-m", "pip", "install"]
            if node.upgrade_requirements:
                command.append("--upgrade")
            command.extend(["-r", str(requirements)])
            constraint = Path("/opt/comfyui-runtime-constraints.txt")
            if constraint.exists():
                command.extend(["-c", str(constraint)])
            self._run_command(command, cwd=destination)

        if node.install_script:
            script = (destination / node.install_script).resolve()
            if destination not in script.parents or not script.is_file():
                raise InstallError(f"Brak bezpiecznego skryptu instalacyjnego: {node.install_script}")
            self._run_command([python, str(script)], cwd=destination)

    def _run_command(self, command: list[str], cwd: Path | None = None) -> None:
        process = subprocess.Popen(
            command,
            cwd=str(cwd) if cwd else None,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            start_new_session=True,
        )
        assert process.stdout is not None
        try:
            for line in iter(process.stdout.readline, ""):
                self._check_cancelled()
                clean = line.strip()
                if clean:
                    self.store.update(message=clean[-240:])
            return_code = process.wait()
            if return_code:
                raise InstallError(f"Polecenie zakończyło się kodem {return_code}: {command[0]}")
        except InstallCancelled:
            os.killpg(process.pid, signal.SIGTERM)
            raise

    def _restart_comfyui(self) -> None:
        if not self.restart_script.exists():
            raise InstallError(f"Brak skryptu restartu: {self.restart_script}")
        self._run_command([str(self.restart_script)])

    @staticmethod
    def _sha256(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _extract(self, archive: Path, destination: Path, kind: str) -> None:
        def safe(member_name: str) -> None:
            member = (destination / member_name).resolve()
            if destination.resolve() not in member.parents and member != destination.resolve():
                raise InstallError(f"Archiwum zawiera niebezpieczną ścieżkę: {member_name}")

        if kind == "zip":
            with zipfile.ZipFile(archive) as bundle:
                for member in bundle.infolist():
                    safe(member.filename)
                bundle.extractall(destination)
        elif kind == "tar":
            with tarfile.open(archive) as bundle:
                for member in bundle.getmembers():
                    safe(member.name)
                bundle.extractall(destination, filter="data")
