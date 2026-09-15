from __future__ import annotations

import hashlib
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
import zipfile
from concurrent.futures import FIRST_EXCEPTION, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

import requests

from .catalog import DownloadSpec, NodeSpec, WorkflowSpec
from .download_engine import DownloadCancelled, DownloadEngine, DownloadError, DownloadRequest
from .instant_models import InstantDownloadSource
from .operations import OperationBusy, OperationCoordinator
from .state import StateStore


NODE_MARKER = ".aimodelki-node-revision"
# Written by the 1.4 and 1.5 images, which baked or installed nodes the same way.
LEGACY_NODE_MARKERS = (".aimodelki-baked-revision",)
DEFAULT_REQUIREMENT_EXCLUDES = Path(__file__).resolve().parents[1] / "docker" / "node-requirements-exclude.txt"
NODE_CONSTRAINTS = (
    Path("/opt/aimodelki-node-constraints.txt"),
    Path("/opt/comfyui-runtime-constraints.txt"),
)
DEFAULT_NODE_ARCHIVE_BASE_URL = "https://pub-746aa51431cf4b7eac8a9cf5e44fbf58.r2.dev/custom-nodes"


def normalized_requirement(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


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
        # Standard Download: one plain request per file and one file at a time, like a browser
        # download. It stays gentle on public hosts such as Hugging Face.
        self.download_engine = DownloadEngine(self.comfyui_root)

        # Instant Download: one file at a time, each over aria2c's 16 long-running range
        # connections, as the fastest RunPod launchers do. rangefetch (many short range
        # requests) is opt-in with AIMODELKI_DOWNLOADER=rangefetch; Python is the fallback.
        downloader = os.getenv("AIMODELKI_DOWNLOADER", "auto").strip().lower()
        rangefetch = shutil.which("rangefetch") if downloader == "rangefetch" else None
        aria2c = shutil.which("aria2c") if downloader in {"auto", "aria2", "rangefetch"} else None
        self.parallel_files = self._env_int("AIMODELKI_DOWNLOAD_PARALLEL_FILES", 1, 1, 8)
        # Segment size and connection count apply to rangefetch and the Python fallback.
        segment_mb = self._env_int("INSTANT_MODELS_DOWNLOAD_SEGMENT_MB", 64, 4, 1024)
        segment_size = segment_mb * 1024 * 1024
        connections = self._env_int("INSTANT_MODELS_DOWNLOAD_CONNECTIONS", 64, 1, 256)
        self.instant_download_engine = DownloadEngine(
            self.comfyui_root,
            parallelism=connections,
            segment_size=segment_size,
            parallel_threshold=segment_size * 2,
            aria2c=aria2c,
            aria2_connections=16,
            rangefetch=rangefetch,
            rangefetch_connections=connections,
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

    def _connection_count(self, instant: bool) -> int:
        if not instant:
            return 1
        engine = self.instant_download_engine
        if engine.rangefetch:
            return self.parallel_files * engine.rangefetch_connections
        if engine.aria2c:
            return self.parallel_files * engine.aria2_connections
        return engine.parallelism

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
                instant = any(item.instant for item in downloads)
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
                    download_mode="instant" if instant else "standard",
                    download_connections=self._connection_count(instant),
                    manual_files_pending=self._manual_payload(pending_manual),
                    error=None,
                    message="Przygotowuję szybkie pobieranie…" if instant else "Przygotowuję instalację…",
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
            self._download_many(downloads, total_items)

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

            for link in workflow.model_links:
                self._apply_model_link(link.source, link.destination)

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

    def _download_many(self, downloads: list[ResolvedDownload], total_items: int) -> None:
        """Download the package files with one aggregate progress.

        Instant Download runs up to ``parallel_files`` files at once; Standard Download
        fetches one file after another.
        """
        if not downloads:
            return
        workers = self.parallel_files if any(item.instant for item in downloads) else 1
        total_expected = sum(item.spec.size_bytes or 0 for item in downloads)
        guard = threading.Lock()
        abort = threading.Event()
        done: dict[int, int] = {}
        speeds: dict[int, int] = {}
        active: dict[int, str] = {}
        finished = 0

        def publish() -> None:
            names = list(active.values())
            label = f"{names[0]} (+{len(names) - 1})" if len(names) > 1 else (names[0] if names else None)
            self._publish_aggregate(
                sum(done.values()), total_expected, sum(speeds.values()), finished, total_items, label
            )

        def run(index: int, resolved: ResolvedDownload) -> None:
            nonlocal finished
            spec = resolved.spec
            with guard:
                active[index] = spec.name
                publish()

            def report(current: int, expected: int, speed: int) -> None:
                with guard:
                    done[index] = min(current, spec.size_bytes or expected or current)
                    speeds[index] = speed
                    publish()

            try:
                size = self._download_one(
                    resolved,
                    report,
                    cancelled=lambda: self._cancel.is_set() or abort.is_set(),
                )
            finally:
                with guard:
                    active.pop(index, None)
                    speeds.pop(index, None)
            with guard:
                done[index] = spec.size_bytes or size
                finished += 1
                publish()

        with ThreadPoolExecutor(max_workers=min(workers, len(downloads))) as executor:
            futures = [executor.submit(run, index, resolved) for index, resolved in enumerate(downloads)]
            completed, _ = wait(futures, return_when=FIRST_EXCEPTION)
            if any(future.exception() for future in completed):
                # Stop the other transfers; their .part files stay resumable.
                abort.set()
                for future in futures:
                    future.cancel()

        errors = [future.exception() for future in futures if not future.cancelled() and future.exception()]
        if self._cancel.is_set():
            raise InstallCancelled()
        failures = [error for error in errors if not isinstance(error, InstallCancelled)]
        if failures:
            raise failures[0]
        if errors:
            raise errors[0]

    def _download_one(
        self,
        resolved: ResolvedDownload,
        report: Callable[[int, int, int], None],
        cancelled: Callable[[], bool],
    ) -> int:
        item = resolved.spec
        request = DownloadRequest(
            name=item.name,
            destination=item.destination,
            size_bytes=item.size_bytes,
            sha256=item.sha256,
            url_provider=resolved.url_provider,
            headers=item.headers,
        )
        engine = self.instant_download_engine if resolved.instant else self.download_engine
        try:
            size = engine.download(request, cancelled=cancelled, progress=report)
        except DownloadCancelled as exc:
            raise InstallCancelled() from exc
        except DownloadError as exc:
            raise InstallError(str(exc)) from exc

        if item.extract:
            self.store.update(message=f"Rozpakowywanie: {item.name}")
            target = self._target(item.destination)
            self._extract(target, target.parent, item.extract)
        return size

    def _download(
        self,
        resolved: ResolvedDownload | DownloadSpec,
        completed_before: int,
        total_expected: int,
        item_index: int,
        total_items: int,
    ) -> int:
        """Download a single file with its own progress; used outside the package flow."""
        if isinstance(resolved, DownloadSpec):
            resolved = ResolvedDownload(resolved, lambda url=resolved.url: url)
        item = resolved.spec

        def report(current: int, expected: int, speed: int) -> None:
            self._publish_aggregate(
                completed_before + min(current, expected or current),
                total_expected or expected,
                speed,
                item_index - 1,
                total_items,
                item.name,
            )

        size = self._download_one(resolved, report, self._cancel.is_set)
        self._publish_aggregate(
            completed_before + (item.size_bytes or size),
            total_expected or (item.size_bytes or size),
            0,
            item_index,
            total_items,
            None,
        )
        return size

    def _publish_aggregate(
        self,
        done: int,
        total: int,
        speed: int,
        finished: int,
        total_items: int,
        label: str | None,
    ) -> None:
        if total:
            progress = min(90, round(done / total * 90))
        else:
            progress = round(finished / max(total_items, 1) * 90)
        changes: dict[str, object] = {
            "status": "downloading",
            "progress": progress,
            "downloaded_bytes": done,
            "total_bytes": total,
            "speed_bytes_per_second": speed,
            "current_item": label,
            "current_index": finished,
            "total_items": total_items,
        }
        # Keep the "stopping" message visible once the user has cancelled.
        if label and not self._cancel.is_set():
            changes["message"] = f"Pobieranie: {label}"
        self.store.update(**changes)

    @staticmethod
    def _marker_revision(destination: Path) -> str | None:
        for name in (NODE_MARKER, *LEGACY_NODE_MARKERS):
            try:
                value = (destination / name).read_text(encoding="ascii").strip().lower()
            except (OSError, UnicodeDecodeError):
                continue
            if value:
                return value
        return None

    def node_revision(self, destination: Path) -> str | None:
        """Checked-out revision of a node directory, without starting git when possible."""
        git_dir = destination / ".git"
        if git_dir.is_dir():
            try:
                head = (git_dir / "HEAD").read_text(encoding="ascii").strip()
            except (OSError, UnicodeDecodeError):
                return None
            # A branch ("ref: refs/heads/main") is not a pinned catalog revision.
            return head.lower() if re.fullmatch(r"[0-9a-fA-F]{40}", head) else None
        if git_dir.exists():
            result = subprocess.run(
                ["git", "-C", str(destination), "rev-parse", "HEAD"],
                capture_output=True,
                text=True,
                check=False,
            )
            return result.stdout.strip().lower() if result.returncode == 0 else None
        # Installed from an archive: the marker is the only record of the revision.
        return self._marker_revision(destination)

    def node_ready(self, node: NodeSpec) -> bool:
        try:
            destination = self._target(node.directory)
        except InstallError:
            return False
        revision = node.revision.lower()
        return self._marker_revision(destination) == revision and self.node_revision(destination) == revision

    def workflow_nodes_ready(self, workflow: WorkflowSpec) -> bool:
        return all(self.node_ready(node) for node in workflow.custom_nodes)

    def _install_node(self, node: NodeSpec) -> None:
        destination = self._target(node.directory)
        # The marker is written only after pip and the install script succeeded.
        if self.node_ready(node):
            return
        if not self._install_node_archive(node, destination):
            self._install_node_git(node, destination)

        python = str(self._comfy_python())
        requirements = destination / "requirements.txt"
        if node.install_requirements and requirements.is_file():
            filtered = self._filtered_requirements(requirements)
            if filtered is not None:
                try:
                    command = [python, "-m", "pip", "install"]
                    if node.upgrade_requirements:
                        command.append("--upgrade")
                    command.extend(["-r", str(filtered)])
                    for constraint in NODE_CONSTRAINTS:
                        if constraint.is_file():
                            command.extend(["-c", str(constraint)])
                    self._run_command(command, cwd=destination)
                finally:
                    filtered.unlink(missing_ok=True)

        if node.install_script:
            script = (destination / node.install_script).resolve()
            if destination not in script.parents or not script.is_file():
                raise InstallError(f"Brak bezpiecznego skryptu instalacyjnego: {node.install_script}")
            self._run_command([python, str(script)], cwd=destination)

        (destination / NODE_MARKER).write_text(node.revision.lower() + "\n", encoding="ascii")

    def _node_archive_url(self, node: NodeSpec) -> str | None:
        base = os.getenv("AIMODELKI_NODE_ARCHIVE_BASE_URL", DEFAULT_NODE_ARCHIVE_BASE_URL).strip().rstrip("/")
        if not base or not node.archive_sha256:
            return None
        return f"{base}/{Path(node.directory).name}-{node.revision.lower()}.tar.gz"

    def _install_node_archive(self, node: NodeSpec, destination: Path) -> bool:
        """Install a node from its pinned R2 archive. False means: use git instead."""
        url = self._node_archive_url(node)
        # A git checkout keeps its own history, and an unknown directory is not ours to replace.
        if not url or (destination / ".git").exists():
            return False
        if destination.exists() and self._marker_revision(destination) is None:
            return False

        cache = self.store.state_dir / "node-archives"
        archive = cache / f"{destination.name}-{node.revision.lower()}.tar.gz"
        staging = destination.parent / f".{destination.name}.staging"
        previous = destination.parent / f".{destination.name}.previous"
        self.store.update(message=f"Pobieranie węzła: {node.name}")
        try:
            cache.mkdir(parents=True, exist_ok=True)
            digest = hashlib.sha256()
            with requests.get(url, stream=True, timeout=(15, 120)) as response:
                response.raise_for_status()
                with archive.open("wb") as handle:
                    for chunk in response.iter_content(1024 * 1024):
                        self._check_cancelled()
                        handle.write(chunk)
                        digest.update(chunk)
            if digest.hexdigest() != node.archive_sha256:
                raise InstallError(f"Suma SHA-256 archiwum węzła {node.name} nie zgadza się")
            shutil.rmtree(staging, ignore_errors=True)
            staging.mkdir(parents=True)
            self._extract(archive, staging, "tar")
            if destination.exists():
                shutil.rmtree(previous, ignore_errors=True)
                destination.replace(previous)
            staging.replace(destination)
            shutil.rmtree(previous, ignore_errors=True)
            return True
        except InstallCancelled:
            raise
        except (requests.RequestException, OSError, tarfile.TarError, InstallError) as exc:
            if isinstance(exc, requests.HTTPError) and exc.response is not None:
                detail = f"HTTP {exc.response.status_code}"
            else:
                detail = type(exc).__name__
            if previous.exists() and not destination.exists():
                previous.replace(destination)
            self.store.update(message=f"Archiwum węzła {node.name} niedostępne ({detail}); pobieram z GitHub…")
            return False
        finally:
            archive.unlink(missing_ok=True)
            shutil.rmtree(staging, ignore_errors=True)

    def _install_node_git(self, node: NodeSpec, destination: Path) -> None:
        git_dir = destination / ".git"
        force = False
        if destination.exists() and not git_dir.exists():
            if self._marker_revision(destination) is None:
                raise InstallError(f"Katalog {node.directory} istnieje, ale nie jest repozytorium git")
            # Installed from an archive earlier: the pinned checkout replaces its files.
            force = True
        destination.mkdir(parents=True, exist_ok=True)
        if not git_dir.exists():
            self._run_command(["git", "-C", str(destination), "init", "-q"])
            self._run_command(["git", "-C", str(destination), "remote", "add", "origin", node.repository])
        # Only the pinned commit is fetched: no history, no tags.
        self._run_command(
            ["git", "-C", str(destination), "fetch", "--depth", "1", "--no-tags", node.repository, node.revision]
        )
        checkout = ["git", "-C", str(destination), "checkout", "--detach"]
        if force:
            checkout.append("--force")
        checkout.append("FETCH_HEAD")
        self._run_command(checkout)

    def _requirement_excludes(self) -> set[str]:
        path = Path(os.getenv("AIMODELKI_NODE_REQUIREMENTS_EXCLUDE", str(DEFAULT_REQUIREMENT_EXCLUDES)))
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return set()
        return {
            normalized_requirement(line.split("#", 1)[0])
            for line in lines
            if line.split("#", 1)[0].strip()
        }

    def _filtered_requirements(self, requirements: Path) -> Path | None:
        """Copy of a node's requirements without packages the image manages itself."""
        excludes = self._requirement_excludes()
        kept: list[str] = []
        for raw in requirements.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.split(" #", 1)[0].strip()
            if not line or line.startswith("#"):
                continue
            # pip options, nested files and VCS or URL requirements are not reproducible.
            if line.startswith("-") or "://" in line or line.startswith(("git+", "file:")):
                continue
            name = re.split(r"[\s<>=!~;\[@(]", line, maxsplit=1)[0]
            if not name or normalized_requirement(name) in excludes:
                continue
            kept.append(line)
        if not kept:
            return None
        handle, name = tempfile.mkstemp(prefix="node-requirements-", suffix=".txt", dir=self.store.state_dir)
        with os.fdopen(handle, "w", encoding="utf-8") as file:
            file.write("\n".join(kept) + "\n")
        return Path(name)

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

    def _apply_model_link(self, source_relative: str, destination_relative: str) -> None:
        source = self._target(source_relative)
        destination = self._target(destination_relative)
        if not source.is_file():
            raise InstallError(f"Brak źródła dowiązania: {source_relative}")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if destination.exists() or destination.is_symlink():
            if destination.resolve() == source.resolve():
                return
            destination.unlink()
        destination.symlink_to(os.path.relpath(source, destination.parent))

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
