import importlib
import sys
from types import SimpleNamespace

from fastapi.testclient import TestClient


def load_app(monkeypatch, tmp_path, token=""):
    monkeypatch.setenv("LAUNCHER_LOAD_DOTENV", "0")
    monkeypatch.setenv("LAUNCHER_STATE_DIR", str(tmp_path / "state"))
    monkeypatch.setenv("INSTANT_MODELS_STATE_DIR", str(tmp_path / "instant-state"))
    monkeypatch.setenv("COMFYUI_ROOT", str(tmp_path / "ComfyUI"))
    monkeypatch.setenv("RUNPOD_LAUNCHER_TOKEN", token)
    monkeypatch.delenv("HF_TOKEN", raising=False)
    sys.modules.pop("launcher.app", None)
    module = importlib.import_module("launcher.app")
    return module


def test_bootstrap_returns_catalog_and_local_service_urls(monkeypatch, tmp_path) -> None:
    module = load_app(monkeypatch, tmp_path)
    client = TestClient(module.app)

    response = client.get("/api/bootstrap")

    assert response.status_code == 200
    payload = response.json()
    assert len(payload["workflows"]) == 4
    assert payload["services"]["comfyui"] == "http://testserver:8188"
    assert payload["services"]["jupyter"] == "http://testserver:8888"
    assert payload["instant_models"]["connected"] is False
    assert payload["workflows"][0]["id"] == "image-generation"
    assert payload["workflows"][0]["configured"] is True
    assert payload["workflows"][0]["file_count"] == 7
    assert payload["workflows"][0]["node_count"] == 4
    workflows = {workflow["id"]: workflow for workflow in payload["workflows"]}
    assert workflows["dataset-generator"]["configured"] is True
    assert workflows["dataset-generator"]["file_count"] == 7
    assert workflows["dataset-generator"]["node_count"] == 1
    assert workflows["image-edit"]["configured"] is True
    assert workflows["image-edit"]["file_count"] == 3
    assert workflows["image-edit"]["node_count"] == 3
    assert workflows["image-edit"]["manual_files"][0]["destination"] == "models/unet/flux-2-klein-9b.safetensors"
    assert workflows["image-edit"]["manual_files"][0]["detected"] is False
    assert workflows["motion-control"]["configured"] is True
    assert workflows["motion-control"]["file_count"] == 6
    assert workflows["motion-control"]["node_count"] == 5
    page = client.get("/")
    assert page.status_code == 200
    assert "<b>AIMODELKI</b> ALL IN ONE" in page.text
    assert "Instant Models" in page.text
    assert page.text.index('class="instant-section"') < page.text.index('class="catalog-section"')
    assert "Sync Models" not in page.text
    assert "Activate Instant Models" in page.text
    assert "Tryb katalogu" not in page.text
    assert 'id="resumeButton"' in page.text
    assert 'href="https://aimodelki.pl/kontakt"' in page.text


def test_instant_models_starts_disconnected_and_rejects_bad_token(monkeypatch, tmp_path) -> None:
    module = load_app(monkeypatch, tmp_path)
    client = TestClient(module.app)

    status_response = client.get("/api/instant-models/status")
    activation_response = client.post("/api/instant-models/activate", json={"token": "invalid"})

    assert status_response.status_code == 200
    assert status_response.json()["connected"] is False
    assert activation_response.status_code == 400
    assert "format" in activation_response.json()["detail"]


def test_active_instant_models_never_falls_back_silently(monkeypatch, tmp_path) -> None:
    module = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(module.instant_models, "status", lambda: {"connected": True})

    def unavailable(_workflow_id):
        raise module.InstantModelsError("API niedostępne")

    monkeypatch.setattr(module.instant_models, "source_for", unavailable)

    response = TestClient(module.app).post("/api/install/image-generation")

    assert response.status_code == 503
    assert "rozłącz token" in response.json()["detail"]


def test_image_edit_remains_available_without_hugging_face_token(monkeypatch, tmp_path) -> None:
    module = load_app(monkeypatch, tmp_path)
    monkeypatch.setenv("HF_TOKEN", "hf_test_token")
    client = TestClient(module.app)

    workflows = {item["id"]: item for item in client.get("/api/bootstrap").json()["workflows"]}

    assert workflows["image-edit"]["configured"] is True
    assert workflows["image-edit"]["missing_env"] == []


def test_runpod_proxy_urls_use_pod_id(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("RUNPOD_POD_ID", "pod-abc123")
    module = load_app(monkeypatch, tmp_path)
    client = TestClient(module.app)

    payload = client.get("/api/bootstrap").json()

    assert payload["services"]["comfyui"] == "https://pod-abc123-8188.proxy.runpod.net"
    assert payload["services"]["jupyter"] == "https://pod-abc123-8888.proxy.runpod.net"


def test_token_protects_mutating_and_bootstrap_endpoints(monkeypatch, tmp_path) -> None:
    module = load_app(monkeypatch, tmp_path, token="secret-token")
    monkeypatch.setattr(
        module.requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {"devices": [{"type": "cuda", "name": "cuda:0"}]},
        ),
    )
    client = TestClient(module.app)

    assert client.get("/api/health").status_code == 200
    assert client.get("/api/bootstrap").status_code == 401
    assert client.get("/api/bootstrap", headers={"X-Launcher-Token": "secret-token"}).status_code == 200


def test_health_requires_comfyui_and_jupyter(monkeypatch, tmp_path) -> None:
    module = load_app(monkeypatch, tmp_path)

    def service_response(url, **_kwargs):
        status_code = 503 if ":8188/" in url else 200
        return SimpleNamespace(
            status_code=status_code,
            json=lambda: {"devices": [{"type": "cuda", "name": "cuda:0"}]},
        )

    monkeypatch.setattr(module.requests, "get", service_response)
    response = TestClient(module.app).get("/api/health")

    assert response.status_code == 503
    payload = response.json()
    assert payload["ok"] is False
    assert payload["checks"]["launcher"]["ok"] is True
    assert payload["checks"]["catalog"]["ok"] is True
    assert payload["checks"]["workspace"]["ok"] is True
    assert payload["checks"]["comfyui"] == {"ok": False, "status_code": 503}
    assert payload["checks"]["jupyter"] == {"ok": True, "status_code": 200}


def test_health_is_ready_when_all_services_respond(monkeypatch, tmp_path) -> None:
    module = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        module.requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(
            status_code=200,
            json=lambda: {"devices": [{"type": "cuda", "name": "cuda:0 NVIDIA GPU"}]},
        ),
    )

    response = TestClient(module.app).get("/api/health")

    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["checks"]["comfyui"]["cuda_devices"] == 1


def test_health_rejects_comfyui_without_cuda_device(monkeypatch, tmp_path) -> None:
    module = load_app(monkeypatch, tmp_path)
    monkeypatch.setattr(
        module.requests,
        "get",
        lambda *args, **kwargs: SimpleNamespace(status_code=200, json=lambda: {"devices": []}),
    )

    response = TestClient(module.app).get("/api/health")

    assert response.status_code == 503
    assert response.json()["checks"]["comfyui"]["cuda_devices"] == 0


def test_unknown_installer_is_rejected(monkeypatch, tmp_path) -> None:
    module = load_app(monkeypatch, tmp_path)
    client = TestClient(module.app)

    response = client.post("/api/install/not-a-package")

    assert response.status_code == 404
