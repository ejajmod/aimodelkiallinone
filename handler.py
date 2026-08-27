from __future__ import annotations

import json
import os
import platform
from pathlib import Path
from typing import Any


SERVICE_NAME = "AIMODELKI ALL IN ONE"
SERVICE_VERSION = os.getenv("AIMODELKI_VERSION", "1.1.0")
SUPPORTED_ACTIONS = ("health", "catalog")


def _catalog_path() -> Path:
    configured = os.getenv("LAUNCHER_CATALOG_PATH")
    if configured:
        return Path(configured)

    baked = Path("/opt/workflow-launcher/catalog/catalog.json")
    if baked.exists():
        return baked
    return Path(__file__).resolve().parent / "catalog" / "catalog.json"


def _load_catalog() -> dict[str, Any]:
    path = _catalog_path()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot load the workflow catalog: {exc}") from exc

    if raw.get("version") != 1 or not isinstance(raw.get("workflows"), list):
        raise ValueError("The workflow catalog must contain version=1 and a workflows array")
    return raw


def _public_workflows(raw: dict[str, Any]) -> list[dict[str, Any]]:
    workflows: list[dict[str, Any]] = []
    for workflow in raw["workflows"]:
        workflows.append(
            {
                "id": str(workflow.get("id", "")),
                "title": str(workflow.get("title", "")),
                "description": str(workflow.get("description", "")),
                "category": str(workflow.get("category", "")),
                "estimated_size_gb": float(workflow.get("estimated_size_gb", 0)),
                "model_file_count": len(workflow.get("downloads", [])),
                "custom_node_count": len(workflow.get("custom_nodes", [])),
            }
        )
    return workflows


def _gpu_summary() -> dict[str, Any]:
    try:
        import torch

        available = bool(torch.cuda.is_available())
        count = int(torch.cuda.device_count()) if available else 0
        names = [torch.cuda.get_device_name(index) for index in range(count)]
        return {"available": available, "count": count, "devices": names}
    except (ImportError, RuntimeError) as exc:
        return {"available": False, "count": 0, "devices": [], "detail": str(exc)}


def _error(code: str, message: str) -> dict[str, Any]:
    return {"ok": False, "error": {"code": code, "message": message}}


def handler(job: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(job, dict):
        return _error("invalid_job", "The job must be a JSON object")

    job_input = job.get("input", {})
    if not isinstance(job_input, dict):
        return _error("invalid_input", "The input field must be a JSON object")

    action = str(job_input.get("action", "health")).strip().lower()
    if action not in SUPPORTED_ACTIONS:
        return _error(
            "unsupported_action",
            f"Supported actions: {', '.join(SUPPORTED_ACTIONS)}",
        )

    try:
        catalog = _load_catalog()
    except ValueError as exc:
        return _error("catalog_unavailable", str(exc))

    workflows = _public_workflows(catalog)
    if action == "catalog":
        return {
            "ok": True,
            "service": SERVICE_NAME,
            "version": SERVICE_VERSION,
            "workflow_count": len(workflows),
            "workflows": workflows,
        }

    return {
        "ok": True,
        "service": SERVICE_NAME,
        "version": SERVICE_VERSION,
        "mode": "serverless",
        "python": platform.python_version(),
        "catalog": {"ok": True, "workflow_count": len(workflows)},
        "gpu": _gpu_summary(),
        "supported_actions": list(SUPPORTED_ACTIONS),
        "note": "Model installation and the browser interfaces are available in Pod mode.",
    }


def main() -> None:
    import runpod

    runpod.serverless.start(
        {
            "handler": handler,
            "concurrency_modifier": lambda _current: 1,
        }
    )


if __name__ == "__main__":
    main()
