from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import urlparse

import requests


ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")
ALLOWED_DOWNLOAD_SCHEMES = {"http", "https"}


class CatalogError(ValueError):
    """Raised when the declarative catalog is unsafe or malformed."""


@dataclass(frozen=True)
class DownloadSpec:
    name: str
    url: str
    destination: str
    size_bytes: int | None = None
    sha256: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    extract: str | None = None


@dataclass(frozen=True)
class NodeSpec:
    name: str
    repository: str
    revision: str
    directory: str
    install_requirements: bool = True
    upgrade_requirements: bool = False
    install_script: str | None = None


@dataclass(frozen=True)
class ManualFileSpec:
    name: str
    destination: str
    size_bytes: int
    sha256: str
    source_url: str


@dataclass(frozen=True)
class WorkflowSpec:
    id: str
    title: str
    description: str
    estimated_size_gb: float
    category: str
    accent: str
    downloads: tuple[DownloadSpec, ...]
    custom_nodes: tuple[NodeSpec, ...]
    required_env: tuple[str, ...]
    manual_files: tuple[ManualFileSpec, ...] = ()

    @property
    def missing_env(self) -> list[str]:
        return [name for name in self.required_env if not os.getenv(name)]

    @property
    def configured(self) -> bool:
        return not self.missing_env and bool(self.downloads or self.custom_nodes)


def _expand_env(value: str) -> str:
    return ENV_PATTERN.sub(lambda match: os.getenv(match.group(1), ""), value)


def _safe_relative_path(value: str, label: str) -> str:
    path = PurePosixPath(value)
    if (
        path.is_absolute()
        or ".." in path.parts
        or not value.strip()
        or "\\" in value
        or re.match(r"^[A-Za-z]:", value)
    ):
        raise CatalogError(f"{label} must be a non-empty path relative to COMFYUI_ROOT")
    return path.as_posix()


def _require_https_or_localhost(url: str, label: str) -> str:
    parsed = urlparse(url)
    if parsed.scheme not in ALLOWED_DOWNLOAD_SCHEMES:
        raise CatalogError(f"{label} must use http or https")
    if parsed.scheme == "http" and parsed.hostname not in {"localhost", "127.0.0.1"}:
        raise CatalogError(f"{label} must use https outside localhost")
    return url


def load_catalog(path: Path) -> list[WorkflowSpec]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise CatalogError(f"Cannot load catalog {path}: {exc}") from exc
    return parse_catalog(raw)


def load_catalog_url(url: str, timeout: int = 20) -> list[WorkflowSpec]:
    _require_https_or_localhost(url, "LAUNCHER_CATALOG_URL")
    response = requests.get(url, timeout=timeout)
    response.raise_for_status()
    return parse_catalog(response.json())


def parse_catalog(raw: dict[str, Any]) -> list[WorkflowSpec]:
    if raw.get("version") != 1 or not isinstance(raw.get("workflows"), list):
        raise CatalogError("Catalog must contain version=1 and a workflows array")

    workflows: list[WorkflowSpec] = []
    seen_ids: set[str] = set()
    for item in raw["workflows"]:
        workflow_id = str(item.get("id", ""))
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{1,63}", workflow_id):
            raise CatalogError(f"Invalid workflow id: {workflow_id!r}")
        if workflow_id in seen_ids:
            raise CatalogError(f"Duplicate workflow id: {workflow_id}")
        seen_ids.add(workflow_id)

        required_env = tuple(str(name) for name in item.get("required_env", []))
        downloads: list[DownloadSpec] = []
        for index, download in enumerate(item.get("downloads", [])):
            label = f"workflows[{workflow_id}].downloads[{index}]"
            url = _expand_env(str(download.get("url", "")))
            if not url and not required_env:
                raise CatalogError(f"{label}.url is empty")
            if url:
                _require_https_or_localhost(url, f"{label}.url")
            checksum = download.get("sha256")
            if checksum and not re.fullmatch(r"[0-9a-fA-F]{64}", str(checksum)):
                raise CatalogError(f"{label}.sha256 must contain 64 hex characters")
            extract = download.get("extract")
            if extract not in {None, "zip", "tar"}:
                raise CatalogError(f"{label}.extract must be zip or tar")
            downloads.append(
                DownloadSpec(
                    name=str(download.get("name") or Path(urlparse(url).path).name or "File"),
                    url=url,
                    destination=_safe_relative_path(str(download.get("destination", "")), f"{label}.destination"),
                    size_bytes=int(download["size_bytes"]) if download.get("size_bytes") else None,
                    sha256=str(checksum).lower() if checksum else None,
                    headers={str(key): _expand_env(str(value)) for key, value in download.get("headers", {}).items()},
                    extract=extract,
                )
            )

        nodes: list[NodeSpec] = []
        for index, node in enumerate(item.get("custom_nodes", [])):
            label = f"workflows[{workflow_id}].custom_nodes[{index}]"
            repository = _expand_env(str(node.get("repository", "")))
            if repository:
                _require_https_or_localhost(repository, f"{label}.repository")
            revision = str(node.get("revision", ""))
            if not revision:
                raise CatalogError(f"{label}.revision is required for reproducibility")
            install_script = node.get("install_script")
            nodes.append(
                NodeSpec(
                    name=str(node.get("name") or "Custom node"),
                    repository=repository,
                    revision=revision,
                    directory=_safe_relative_path(str(node.get("directory", "")), f"{label}.directory"),
                    install_requirements=bool(node.get("install_requirements", True)),
                    upgrade_requirements=bool(node.get("upgrade_requirements", False)),
                    install_script=(
                        _safe_relative_path(str(install_script), f"{label}.install_script")
                        if install_script
                        else None
                    ),
                )
            )

        manual_files: list[ManualFileSpec] = []
        for index, manual in enumerate(item.get("manual_files", [])):
            label = f"workflows[{workflow_id}].manual_files[{index}]"
            checksum = str(manual.get("sha256", "")).lower()
            if not re.fullmatch(r"[0-9a-f]{64}", checksum):
                raise CatalogError(f"{label}.sha256 must contain 64 hex characters")
            try:
                size_bytes = int(manual.get("size_bytes", 0))
            except (TypeError, ValueError) as exc:
                raise CatalogError(f"{label}.size_bytes must be positive") from exc
            if size_bytes <= 0:
                raise CatalogError(f"{label}.size_bytes must be positive")
            manual_files.append(
                ManualFileSpec(
                    name=str(manual.get("name") or "Manual model"),
                    destination=_safe_relative_path(str(manual.get("destination", "")), f"{label}.destination"),
                    size_bytes=size_bytes,
                    sha256=checksum,
                    source_url=_require_https_or_localhost(str(manual.get("source_url", "")), f"{label}.source_url"),
                )
            )

        workflows.append(
            WorkflowSpec(
                id=workflow_id,
                title=str(item.get("title") or workflow_id),
                description=str(item.get("description") or ""),
                estimated_size_gb=float(item.get("estimated_size_gb", 0)),
                category=str(item.get("category") or "Workflow"),
                accent=str(item.get("accent") or "indigo"),
                downloads=tuple(downloads),
                custom_nodes=tuple(nodes),
                required_env=required_env,
                manual_files=tuple(manual_files),
            )
        )
    return workflows
