from __future__ import annotations

import os
import signal
import subprocess
import sys
import tarfile
import threading
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .catalog import DownloadSpec, NodeSpec, WorkflowSpec
from .download_engine import DownloadCancelled, DownloadEngine, DownloadError, DownloadRequest
from .instant_models import InstantDownloadSource
from .operations import OperationBusy, OperationCoordinator
from .state import StateStore


class InstallCancelled(RuntimeError):
    pass


class InstallError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResolvedDownload:
    spec: DownloadSpec
    url_provider: Callable[[], str]
    instant: bool = False


class Installer:
    ACTIVE_STATES = {"queued", "downloading", "installing", "restarting"}

    def __init__(
        self,
        comfyui_root: Path,
        store: StateStore,
        restart_script: Path,
        coordinator: OperationCoordinator | None = None,
    ):
        self.comfyui_root = comfyui_root.resolve()
        self.store = store
        self.restart_script = restart_script
        self._thread: threading.Thread | None = None
        self._cancel = threading.Event()
        self._guard = threading.Lock()
        self.coordinator = coordinator or OperationCoordinator()
        self.download_engine = DownloadEngine(self.comfyui_root)
        connections = self._env_int("INSTANT_MODELS_DOWNLOAD_CONNECTIONS", 64, 1, 64)
        segment_mb = self._env_int("INSTANT_MODELS_DOWNLOAD_SEGMENT_MB", 64, 8, 1024)
        segment_size = segment_mb * 1024 * 1024
        self.instant_download_engine = DownloadEngine(
            self.comfyui_root,
            parallelism=connections,
            segment_size=segment_size,
            parallel_threshold=segment_size * 2,
        )

    @staticmethod
    def _env_int(name: str, default: int, minimum: int, maximum: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
        except ValueError:
            value = default
        return max(minimum, min(value, maximum))

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
        return next((candidate for candidate in candidates if candidate.is_file()), Path(sys.executable))

    def start(
        self,
        workflow: WorkflowSpec,
        instant_source: InstantDownloadSource | None = None,
    ) -> None:
        with self._guard:
            if self.active:
                raise InstallError("Inny instalator jest już uruchomiony")
            if not workflow.configured:
                missing = ", ".join(workflow.missing_env) or "downloads/custom_nodes"
                raise InstallError(f"Instalator wymaga konfiguracji: {missing}")
            comfy_python = self._comfy_python()
            if not (self.comfyui_root / "main.py").is_file() or not comfy_python.is_file():
                raise InstallError("ComfyUI jest jeszcze inicjalizowane. Poczekaj chwilę i spróbuj ponownie.")
            try:
                self.coordinator.acquire("workflow")
            except OperationBusy as exc:
                raise InstallError(str(exc)) from exc
            self._cancel.clear()
            try:
                downloads, pending_manual = self._resolve_downloads(workflow, instant_source)
                self.store.update(
                    status="queued",
                    workflow_id=workflow.id,
                    workflow_title=workflow.title,
                    progress=0,
                    current_item=None,
                    current_index=0,
                    total_items=len(downloads) + len(workflow.custom_nodes),
                    downloaded_bytes=0,
                    total_bytes=sum(item.spec.size_bytes or 0 for item in downloads),
                    speed_bytes_per_second=0,
                    download_mode="instant" if any(item.instant for item in downloads) else "standard",
                    download_connections=self.instant_download_engine.parallelism if any(item.instant for item in downloads) else 1,
                    manual_files_pending=self._manual_payload(pending_manual),
                    error=None,
                    message=(
                        "Przygotowuję szybkie pobieranie…"
                        if any(item.instant for item in downloads)
                        else "Przygotowuję instalację…"
                    ),
                )
                self._thread = threading.Thread(
                    target=self._run,
                    args=(workflow, downloads, pending_manual),
                    daemon=True,
                )
                self._thread.start()
            except Exception:
                self.coordinator.release("workflow")
                raise

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

    def _resolve_downloads(
        self,
        workflow: WorkflowSpec,
        instant_source: InstantDownloadSource | None,
    ) -> tuple[list[ResolvedDownload], list[object]]:
        downloads: list[ResolvedDownload] = []
        for item in workflow.downloads:
            remote = instant_source.file_for(item.destination) if instant_source else None
            if instant_source and not remote:
                raise InstallError(f"Manifest Instant Models nie zawiera pliku pakietu: {item.name}")
            if remote:
                if remote.size != item.size_bytes or remote.sha256.lower() != (item.sha256 or "").lower():
                    raise InstallError(f"Manifest Instant Models nie zgadza się z katalogiem: {item.name}")
                downloads.append(
                    ResolvedDownload(
                        item,
                        lambda file_id=remote.id: instant_source.client.download_url(file_id),
                        instant=True,
                    )
                )
            else:
                downloads.append(ResolvedDownload(item, lambda url=item.url: url))

        pending_manual = []
        for manual in workflow.manual_files:
            remote = instant_source.file_for(manual.destination) if instant_source else None
            if not remote:
                if instant_source:
                    raise InstallError(f"Manifest Instant Models nie zawiera pliku pakietu: {manual.name}")
                pending_manual.append(manual)
                continue
            if remote.size != manual.size_bytes or remote.sha256.lower() != manual.sha256.lower():
                raise InstallError(f"Manifest Instant Models nie zgadza się z katalogiem: {manual.name}")
            downloads.append(
                ResolvedDownload(
                    DownloadSpec(
                        name=manual.name,
                        url="",
                        destination=manual.destination,
                        size_bytes=manual.size_bytes,
                        sha256=manual.sha256,
                        headers={},
                        extract=None,
                    ),
                    lambda file_id=remote.id: instant_source.client.download_url(file_id),
                    instant=True,
                )
            )
        return downloads, pending_manual

    def _manual_payload(self, files: list[object]) -> list[dict[str, object]]:
        return [
            {
                "name": file.name,
                "destination": file.destination,
                "full_path": str(self._target(file.destination)),
                "size_bytes": file.size_bytes,
                "sha256": file.sha256,
                "source_url": file.source_url,
                "detected": False,
            }
            for file in files
        ]

    def _run(
        self,
        workflow: WorkflowSpec,
        downloads: list[ResolvedDownload],
        pending_manual: list[object],
    ) -> None:
        try:
            self.comfyui_root.mkdir(parents=True, exist_ok=True)
            total_items = len(downloads) + len(workflow.custom_nodes)
            total_expected = sum(item.spec.size_bytes or 0 for item in downloads)
            completed_expected = 0

            for index, resolved in enumerate(downloads, start=1):
                self._check_cancelled()
                item = resolved.spec
                self.store.update(
                    status="downloading",
                    current_item=item.name,
                    current_index=index,
                    total_items=total_items,
                    message=f"Pobieranie: {item.name}",
                )
                actual_size = self._download(resolved, completed_expected, total_expected, index, total_items)
                completed_expected += item.size_bytes or actual_size

            node_base = len(downloads)
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
                message=(
                    "Pliki pomocnicze są gotowe. Dodaj ręcznie główny model wskazany poniżej i uruchom ComfyUI ponownie."
                    if pending_manual
                    else "Pakiet jest gotowy. ComfyUI zostało uruchomione ponownie."
                ),
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
        finally:
            self.coordinator.release("workflow")

    def _download(
        self,
        resolved: ResolvedDownload | DownloadSpec,
        completed_before: int,
        total_expected: int,
        item_index: int,
        total_items: int,
    ) -> int:
        if isinstance(resolved, DownloadSpec):
            resolved = ResolvedDownload(resolved, lambda url=resolved.url: url)
        item = resolved.spec
        request = DownloadRequest(
            name=item.name,
            destination=item.destination,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            url_provider=resolved.url_provider,
            headers=item.headers,
        )

        def publish(current: int, expected: int, speed: int) -> None:
            aggregate_total = total_expected or expected
            aggregate_done = completed_before + min(current, expected or current)
            self._publish_download_progress(
                item,
                aggregate_done,
                aggregate_total,
                current,
                speed,
                item_index,
                total_items,
            )

        try:
            engine = self.instant_download_engine if resolved.instant else self.download_engine
            size = engine.download(
                request,
                cancelled=self._cancel.is_set,
                progress=publish,
            )
        except DownloadCancelled as exc:
            raise InstallCancelled() from exc
        except DownloadError as exc:
            raise InstallError(str(exc)) from exc

        target = self._target(item.destination)

        if item.extract:
            self.store.update(message=f"Rozpakowywanie: {item.name}")
            self._extract(target, target.parent, item.extract)

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
        baked_revision = destination / ".aimodelki-baked-revision"
        current_revision = None
        if destination.exists() and baked_revision.is_file():
            current_revision = subprocess.run(
                ["git", "-C", str(destination), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
        preinstalled = (
            current_revision is not None
            and current_revision.returncode == 0
            and current_revision.stdout.strip() == node.revision
            and baked_revision.read_text(encoding="ascii").strip() == node.revision
        )
        if not preinstalled:
            if not destination.exists():
                destination.parent.mkdir(parents=True, exist_ok=True)
                self._run_command(["git", "clone", "--filter=blob:none", node.repository, str(destination)])
            self._run_command(["git", "-C", str(destination), "fetch", "--depth", "1", "origin", node.revision])
            self._run_command(["git", "-C", str(destination), "checkout", "--detach", "FETCH_HEAD"])

        python = str(self._comfy_python())
        requirements = destination / "requirements.txt"
        if not preinstalled and node.install_requirements and requirements.exists():
            command = [python, "-m", "pip", "install"]
            if node.upgrade_requirements:
                command.append("--upgrade")
            command.extend(["-r", str(requirements)])
            constraint = Path("/opt/comfyui-runtime-constraints.txt")
            if constraint.exists():
                command.extend(["-c", str(constraint)])
            self._run_command(command, cwd=destination)

        if node.install_script and not preinstalled:
            script = (destination / node.install_script).resolve()
            if destination not in script.parents or not script.is_file():
                raise InstallError(f"Brak bezpiecznego skryptu instalacyjnego: {node.install_script}")
            self._run_command([python, str(script)], cwd=destination)

        if not preinstalled:
            baked_revision.write_text(node.revision + "\n", encoding="ascii")

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
