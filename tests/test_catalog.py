import os
import re
from pathlib import Path

import pytest

from launcher.catalog import CatalogError, load_catalog, parse_catalog


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def test_environment_url_controls_configuration(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MODEL_URL", raising=False)
    raw = {
        "version": 1,
        "workflows": [
            {
                "id": "image-pack",
                "title": "Image pack",
                "required_env": ["MODEL_URL"],
                "downloads": [
                    {
                        "url": "${MODEL_URL}",
                        "destination": "models/checkpoints/model.safetensors",
                    }
                ],
            }
        ],
    }
    workflow = parse_catalog(raw)[0]
    assert workflow.configured is False
    assert workflow.missing_env == ["MODEL_URL"]

    monkeypatch.setenv("MODEL_URL", "https://example.com/model.safetensors")
    workflow = parse_catalog(raw)[0]
    assert workflow.configured is True


@pytest.mark.parametrize("destination", ["/root/file", "../escape", "models/../../escape", ""])
def test_unsafe_destination_is_rejected(destination: str) -> None:
    raw = {
        "version": 1,
        "workflows": [
            {
                "id": "unsafe-pack",
                "downloads": [{"url": "https://example.com/a", "destination": destination}],
            }
        ],
    }
    with pytest.raises(CatalogError):
        parse_catalog(raw)


def test_http_requires_localhost() -> None:
    raw = {
        "version": 1,
        "workflows": [
            {
                "id": "unsafe-url",
                "downloads": [{"url": "http://example.com/a", "destination": "models/a"}],
            }
        ],
    }
    with pytest.raises(CatalogError):
        parse_catalog(raw)


def test_custom_node_install_script_is_relative() -> None:
    raw = {
        "version": 1,
        "workflows": [
            {
                "id": "node-pack",
                "custom_nodes": [
                    {
                        "repository": "https://github.com/example/node.git",
                        "revision": "0123456789abcdef",
                        "directory": "custom_nodes/node",
                        "install_script": "../outside.py",
                    }
                ],
            }
        ],
    }
    with pytest.raises(CatalogError):
        parse_catalog(raw)


def test_custom_node_can_request_requirements_upgrade() -> None:
    raw = {
        "version": 1,
        "workflows": [
            {
                "id": "node-pack",
                "custom_nodes": [
                    {
                        "repository": "https://github.com/example/node.git",
                        "revision": "0123456789abcdef",
                        "directory": "custom_nodes/node",
                        "upgrade_requirements": True,
                    }
                ],
            }
        ],
    }

    node = parse_catalog(raw)[0].custom_nodes[0]

    assert node.install_requirements is True
    assert node.upgrade_requirements is True


def test_production_catalog_pins_every_artifact() -> None:
    workflows = load_catalog(PROJECT_ROOT / "catalog" / "catalog.json")

    assert {workflow.id for workflow in workflows} == {
        "image-generation",
        "dataset-generator",
        "image-edit",
        "motion-control",
    }
    for workflow in workflows:
        for download in workflow.downloads:
            assert "/resolve/main/" not in download.url
            assert download.size_bytes and download.size_bytes > 0
            assert download.sha256 and re.fullmatch(r"[0-9a-f]{64}", download.sha256)
        for node in workflow.custom_nodes:
            assert re.fullmatch(r"[0-9a-f]{40}", node.revision)


def test_image_edit_catalog_matches_production_workflow_model_names() -> None:
    workflows = load_catalog(PROJECT_ROOT / "catalog" / "catalog.json")
    image_edit = next(workflow for workflow in workflows if workflow.id == "image-edit")

    assert {Path(download.destination).name for download in image_edit.downloads} == {
        "flux-2-klein-9b.safetensors",
        "qwen_3_8b_fp8mixed.safetensors",
        "flux2-vae.safetensors",
        "breast_slider_9b_klein_20260118_210913.safetensors",
    }
    assert {node.revision for node in image_edit.custom_nodes} == {
        "8ff50e4521881eca1fe26aec9615fc9362474931",
        "070001b36be2bbdfcf766b6a2d6f14c36cb62906",
        "3d53de09d8c1fb904d5cb0a137b9bc75e9d77108",
    }
