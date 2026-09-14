from __future__ import annotations

import os
import shutil
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import dotenv_values
from pydantic import BaseModel

from . import __version__
from .catalog import CatalogError, WorkflowSpec, load_catalog, load_catalog_url
from .installer import InstallError, Installer
from .instant_models import InstantModelsError, InstantModelsManager
from .operations import OperationCoordinator
from .state import StateStore


DEFAULT_APP_ROOT = Path(__file__).resolve().parents[1]
if os.getenv("LAUNCHER_LOAD_DOTENV", "1").strip().lower() not in {"0", "false", "no"}:
    for key, value in dotenv_values(DEFAULT_APP_ROOT / ".env").items():
        if value is not None and not os.getenv(key):
            os.environ[key] = value

APP_ROOT = Path(os.getenv("LAUNCHER_APP_ROOT", DEFAULT_APP_ROOT))
STATIC_DIR = APP_ROOT / "launcher" / "static"
CATALOG_PATH = Path(os.getenv("LAUNCHER_CATALOG_PATH", APP_ROOT / "catalog" / "catalog.json"))
STATE_DIR = Path(os.getenv("LAUNCHER_STATE_DIR", "/workspace/.aimodelki-allinone"))
COMFYUI_ROOT = Path(os.getenv("COMFYUI_ROOT", "/workspace/runpod-slim/ComfyUI"))
RESTART_SCRIPT = APP_ROOT / "scripts" / "restart-comfyui.sh"


def _read_catalog() -> list[WorkflowSpec]:
    url = os.getenv("LAUNCHER_CATALOG_URL", "").strip()
    return load_catalog_url(url) if url else load_catalog(CATALOG_PATH)


catalog_error: str | None = None
try:
    workflows = _read_catalog()
except (CatalogError, OSError, ValueError) as exc:
    workflows = []
    catalog_error = str(exc)

store = StateStore(STATE_DIR)
coordinator = OperationCoordinator()
instant_models = InstantModelsManager(
    Path(os.getenv("INSTANT_MODELS_STATE_DIR", "/workspace/.instant-models")),
    os.getenv("INSTANT_MODELS_API_URL", "https://app.aimodelki.pl/api/v1/instant-models"),
)
installer = Installer(COMFYUI_ROOT, store, RESTART_SCRIPT, coordinator)
app = FastAPI(title="AIMODELKI ALL IN ONE", version=__version__, docs_url=None, redoc_url=None)


class InstantActivation(BaseModel):
    token: str


def _probe_service(url: str) -> dict[str, object]:
    try:
        response = requests.get(url, timeout=(0.5, 1.5))
        return {"ok": response.status_code == 200, "status_code": response.status_code}
    except requests.RequestException as exc:
        return {"ok": False, "error": type(exc).__name__}


def _probe_comfyui() -> dict[str, object]:
    try:
        response = requests.get("http://127.0.0.1:8188/system_stats", timeout=(0.5, 1.5))
    except requests.RequestException as exc:
        return {"ok": False, "error": type(exc).__name__}
    if response.status_code != 200:
        return {"ok": False, "status_code": response.status_code}
    try:
        payload = response.json()
    except (ValueError, TypeError):
        return {"ok": False, "status_code": response.status_code, "error": "InvalidJSON"}
    devices = payload.get("devices", []) if isinstance(payload, dict) else []
    cuda_devices = [
        device
        for device in devices
        if isinstance(device, dict)
        and (
            str(device.get("type", "")).lower() == "cuda"
            or "cuda" in str(device.get("name", "")).lower()
        )
    ]
    return {
        "ok": bool(cuda_devices),
        "status_code": response.status_code,
        "cuda_devices": len(cuda_devices),
    }


def _probe_workspace() -> dict[str, object]:
    try:
        usage = shutil.disk_usage(STATE_DIR)
        writable = STATE_DIR.is_dir() and os.access(STATE_DIR, os.W_OK)
        return {
            "ok": writable and usage.free >= 1024**3,
            "writable": writable,
            "free_gb": round(usage.free / 1024**3, 1),
        }
    except OSError as exc:
        return {"ok": False, "error": type(exc).__name__}


def require_token(x_launcher_token: str | None = Header(default=None)) -> None:
    expected = os.getenv("RUNPOD_LAUNCHER_TOKEN", "")
    if expected and x_launcher_token != expected:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Nieprawidłowy token launchera")


def public_service_url(port: int, request: Request) -> str:
    pod_id = os.getenv("RUNPOD_POD_ID") or os.getenv("RUNPOD_PODID")
    if pod_id:
        return f"https://{pod_id}-{port}.proxy.runpod.net"
    hostname = request.url.hostname or "localhost"
    scheme = request.url.scheme
    if hostname.endswith(".proxy.runpod.net"):
        first, suffix = hostname.split(".", 1)
        pod_prefix = first.rsplit("-", 1)[0]
        return f"https://{pod_prefix}-{port}.{suffix}"
    return f"{scheme}://{hostname}:{port}"


@app.get("/api/health")
def health() -> JSONResponse:
    checks = {
        "launcher": {"ok": True},
        "catalog": {"ok": catalog_error is None, "error": catalog_error},
        "workspace": _probe_workspace(),
        "comfyui": _probe_comfyui(),
        "jupyter": _probe_service("http://127.0.0.1:8888/api/"),
        "instant_models": {
            "ok": True,
            "required": False,
            "connected": bool(instant_models.status().get("connected")),
            "status": instant_models.status().get("status", "idle"),
        },
    }
    ready = all(bool(check["ok"]) for check in checks.values())
    return JSONResponse(
        status_code=status.HTTP_200_OK if ready else status.HTTP_503_SERVICE_UNAVAILABLE,
        content={"ok": ready, "version": app.version, "checks": checks},
    )


@app.get("/api/bootstrap", dependencies=[Depends(require_token)])
def bootstrap(request: Request) -> dict[str, object]:
    state = store.get()
    installed = set(state.get("installed_workflows", []))
    comfyui_root = COMFYUI_ROOT.resolve()

    def manual_file_status(workflow: WorkflowSpec) -> list[dict[str, object]]:
        files: list[dict[str, object]] = []
        for item in workflow.manual_files:
            target = (comfyui_root / item.destination).resolve()
            inside_root = comfyui_root in target.parents
            try:
                detected = inside_root and target.is_file() and target.stat().st_size == item.size_bytes
            except OSError:
                detected = False
            files.append(
                {
                    "name": item.name,
                    "destination": item.destination,
                    "full_path": str(comfyui_root / item.destination),
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                    "source_url": item.source_url,
                    "detected": detected,
                }
            )
        return files

    items = [
        {
            "id": workflow.id,
            "title": workflow.title,
            "description": workflow.description,
            "estimated_size_gb": workflow.estimated_size_gb,
            "category": workflow.category,
            "accent": workflow.accent,
            "configured": workflow.configured,
            "missing_env": workflow.missing_env,
            "installed": workflow.id in installed or (state.get("workflow_id") == workflow.id and state.get("status") == "complete"),
            "file_count": len(workflow.downloads),
            "node_count": len(workflow.custom_nodes),
            "manual_files": manual_file_status(workflow),
        }
        for workflow in workflows
    ]
    return {
        "workflows": items,
        "job": state,
        "catalog_error": catalog_error,
        "services": {
            "comfyui": public_service_url(8188, request),
            "jupyter": public_service_url(8888, request),
        },
        "instant_models": instant_models.status(),
    }


@app.post("/api/install/{workflow_id}", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_token)])
def install(workflow_id: str) -> dict[str, object]:
    workflow = next((item for item in workflows if item.id == workflow_id), None)
    if not workflow:
        raise HTTPException(status_code=404, detail="Nie znaleziono pakietu")
    instant_source = None
    if instant_models.status().get("connected"):
        try:
            instant_source = instant_models.source_for(workflow.id)
        except InstantModelsError as exc:
            raise HTTPException(
                status_code=503,
                detail=(
                    f"Instant Models jest aktywny, ale szybkie źródło jest niedostępne: {exc}. "
                    "Spróbuj ponownie albo rozłącz token, aby użyć Standard Download."
                ),
            ) from exc
    try:
        installer.start(workflow, instant_source)
    except InstallError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"accepted": True, "job": store.get()}


@app.post("/api/jobs/current/cancel", dependencies=[Depends(require_token)])
def cancel() -> dict[str, object]:
    try:
        installer.cancel()
    except InstallError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"accepted": True, "job": store.get()}


@app.get("/api/jobs/current", dependencies=[Depends(require_token)])
def current_job() -> dict[str, object]:
    return store.get()


@app.get("/api/instant-models/status", dependencies=[Depends(require_token)])
def instant_status() -> dict[str, object]:
    return instant_models.status()


@app.post("/api/instant-models/activate", dependencies=[Depends(require_token)])
def instant_activate(payload: InstantActivation) -> dict[str, object]:
    if installer.active:
        raise HTTPException(status_code=409, detail="Poczekaj na zakończenie aktywnego instalatora")
    try:
        return instant_models.activate(payload.token)
    except InstantModelsError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.delete("/api/instant-models/connection", dependencies=[Depends(require_token)])
def instant_disconnect() -> dict[str, object]:
    if installer.active:
        raise HTTPException(status_code=409, detail="Poczekaj na zakończenie aktywnego instalatora")
    try:
        return instant_models.disconnect()
    except InstantModelsError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc


app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
