from __future__ import annotations

import json
from pathlib import Path

import handler as hub_handler


def test_health_reports_catalog_and_capabilities(monkeypatch, tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "version": 1,
                "workflows": [
                    {
                        "id": "image-generation",
                        "title": "Image Generation",
                        "description": "Test workflow",
                        "category": "Image",
                        "estimated_size_gb": 1.5,
                        "downloads": [{"url": "https://example.com/model"}],
                        "custom_nodes": [{"repository": "https://example.com/node"}],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LAUNCHER_CATALOG_PATH", str(catalog))
    monkeypatch.setattr(hub_handler, "_gpu_summary", lambda: {"available": True, "count": 1, "devices": ["GPU"]})

    result = hub_handler.handler({"input": {"action": "health"}})

    assert result["ok"] is True
    assert result["mode"] == "serverless"
    assert result["catalog"] == {"ok": True, "workflow_count": 1}
    assert result["gpu"]["count"] == 1
    assert result["supported_actions"] == ["health", "catalog"]


def test_catalog_returns_only_public_metadata(monkeypatch, tmp_path: Path) -> None:
    catalog = tmp_path / "catalog.json"
    catalog.write_text(
        json.dumps(
            {
                "version": 1,
                "workflows": [
                    {
                        "id": "image-edit",
                        "title": "Image Edit",
                        "description": "Test workflow",
                        "category": "Image",
                        "estimated_size_gb": 25.3,
                        "downloads": [
                            {
                                "url": "https://example.com/private-model",
                                "headers": {"Authorization": "Bearer secret"},
                            }
                        ],
                        "custom_nodes": [],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("LAUNCHER_CATALOG_PATH", str(catalog))

    result = hub_handler.handler({"input": {"action": "catalog"}})

    assert result["ok"] is True
    assert result["workflow_count"] == 1
    assert result["workflows"][0]["id"] == "image-edit"
    assert result["workflows"][0]["model_file_count"] == 1
    assert "url" not in result["workflows"][0]
    assert "headers" not in result["workflows"][0]


def test_handler_rejects_invalid_input() -> None:
    assert hub_handler.handler({"input": "bad"})["error"]["code"] == "invalid_input"
    assert hub_handler.handler({"input": {"action": "install"}})["error"]["code"] == "unsupported_action"


def test_handler_reports_missing_catalog(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("LAUNCHER_CATALOG_PATH", str(tmp_path / "missing.json"))

    result = hub_handler.handler({"input": {"action": "health"}})

    assert result["ok"] is False
    assert result["error"]["code"] == "catalog_unavailable"
