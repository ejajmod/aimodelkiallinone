from __future__ import annotations

import os
import shutil
from pathlib import Path

import requests
from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from dotenv import dotenv_values

from .catalog import CatalogError, WorkflowSpec, load_catalog, load_catalog_url
from .installer import InstallError, Installer
from .state import StateStore


DEFAULT_APP_ROOT = Path(__file__).resolve().parents[1]
PUBLIC_HF_TOKEN = "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"
if os.getenv("LAUNCHER_LOAD_DOTENV", "1").strip().lower() not in {"0", "false", "no"}:
    for key, value in dotenv_values(DEFAULT_APP_ROOT / ".env").items():
        if value is not None and not os.getenv(key):
            os.environ[key] = value

if not os.getenv("HF_TOKEN", "").strip():
    os.environ["HF_TOKEN"] = PUBLIC_HF_TOKEN

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
installer = Installer(COMFYUI_ROOT, store, RESTART_SCRIPT)
app = FastAPI(title="AIMODELKI ALL IN ONE", version="1.0.0", docs_url=None, redoc_url=None)


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
    }


@app.post("/api/install/{workflow_id}", status_code=status.HTTP_202_ACCEPTED, dependencies=[Depends(require_token)])
def install(workflow_id: str) -> dict[str, object]:
    workflow = next((item for item in workflows if item.id == workflow_id), None)
    if not workflow:
        raise HTTPException(status_code=404, detail="Nie znaleziono pakietu")
    try:
        installer.start(workflow)
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


app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


@app.get("/{path:path}", include_in_schema=False)
def frontend(path: str) -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")
