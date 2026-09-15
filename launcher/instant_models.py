from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urljoin, urlparse

import requests

from .state import StateStore


class InstantModelsError(RuntimeError):
    pass


@dataclass(frozen=True)
class InstantFile:
    id: str
    filename: str
    size: int
    sha256: str
    destination: str


@dataclass(frozen=True)
class InstantBundle:
    id: str
    title: str
    description: str
    total_size: int
    files: tuple[InstantFile, ...]


@dataclass(frozen=True)
class InstantManifest:
    version: str
    valid_until: str
    bundles: tuple[InstantBundle, ...]


@dataclass(frozen=True)
class InstantDownloadSource:
    bundle: InstantBundle
    client: "InstantModelsClient"

    def file_for(self, destination: str) -> InstantFile | None:
        normalized = destination.replace("\\", "/").removeprefix("models/")
        return next(
            (item for item in self.bundle.files if item.destination.replace("\\", "/").removeprefix("models/") == normalized),
            None,
        )


class CredentialsStore:
    def __init__(self, root: Path):
        self.root = root
        self.path = root / "credentials"

    def save(self, token: str) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(self.root, 0o700)
        except OSError:
            pass
        temporary = self.path.with_suffix(".tmp")
        temporary.write_text(json.dumps({"token": token}), encoding="utf-8")
        try:
            os.chmod(temporary, 0o600)
        except OSError:
            pass
        temporary.replace(self.path)

    def load(self) -> str | None:
        try:
            value = json.loads(self.path.read_text(encoding="utf-8")).get("token")
            return value.strip() if isinstance(value, str) and value.strip() else None
        except (OSError, json.JSONDecodeError, AttributeError):
            return None

    def delete(self) -> None:
        self.path.unlink(missing_ok=True)

    def masked(self) -> str | None:
        token = self.load()
        if not token:
            return None
        return f"{token[:16]}••••••••{token[-4:]}"


class InstantModelsClient:
    def __init__(self, base_url: str, token: str, session: requests.Session | None = None):
        self.base_url = base_url.rstrip("/") + "/"
        parsed = urlparse(self.base_url)
        if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
            raise InstantModelsError("Instant Models API musi używać HTTPS")
        self.token = token
        self.session = session or requests.Session()

    RETRYABLE_STATUS = {502, 503, 504}
    ATTEMPTS = 3

    @staticmethod
    def _server_detail(response: requests.Response) -> str:
        """The API's own error text, or the content type when it did not send JSON."""
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict) and isinstance(payload.get("error"), str):
            return payload["error"][:200]
        return f"odpowiedź {response.headers.get('Content-Type') or 'bez typu'}"

    def _request(self, method: str, relative: str) -> dict[str, Any]:
        response: requests.Response | None = None
        for attempt in range(self.ATTEMPTS):
            if attempt:
                time.sleep(2**attempt)
            try:
                response = self.session.request(
                    method,
                    urljoin(self.base_url, relative.lstrip("/")),
                    headers={"Authorization": f"Bearer {self.token}"},
                    timeout=(10, 45),
                )
            except requests.RequestException as exc:
                if attempt + 1 == self.ATTEMPTS:
                    raise InstantModelsError("Centralne Instant Models API jest niedostępne") from exc
                continue
            # A restarting or briefly overloaded service answers 502-504; ask again.
            if response.status_code not in self.RETRYABLE_STATUS:
                break
        assert response is not None
        if response.status_code in {401, 402, 403}:
            message = {
                401: "Token Instant Models jest nieprawidłowy",
                402: "Subskrypcja Instant Models jest nieaktywna",
                403: "Token nie ma dostępu do tego pakietu",
            }[response.status_code]
            raise InstantModelsError(message)
        if response.status_code == 429:
            raise InstantModelsError("Zbyt wiele żądań do Instant Models API")
        detail = self._server_detail(response)
        if response.status_code == 404:
            raise InstantModelsError(
                f"Instant Models API nie zna zasobu {relative} (HTTP 404: {detail}). "
                "Manifest na serwerze nie zawiera tego pliku lub pakietu."
            )
        if not response.ok:
            raise InstantModelsError(
                f"Instant Models API zwróciło błąd HTTP {response.status_code} dla {relative}: {detail}"
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise InstantModelsError(
                f"Instant Models API zwróciło nieprawidłową odpowiedź dla {relative} (HTTP {response.status_code}, {detail})"
            ) from exc
        if not isinstance(payload, dict):
            raise InstantModelsError(f"Instant Models API zwróciło nieprawidłową odpowiedź dla {relative}")
        return payload

    def manifest(self) -> InstantManifest:
        payload = self._request("GET", "manifest")
        bundles: list[InstantBundle] = []
        seen_files: dict[str, InstantFile] = {}
        destinations: dict[str, str] = {}
        for raw_bundle in payload.get("bundles", []):
            if not isinstance(raw_bundle, dict):
                raise InstantModelsError("Manifest zawiera nieprawidłowy pakiet")
            files: list[InstantFile] = []
            for raw_file in raw_bundle.get("files", []):
                try:
                    file = InstantFile(
                        id=str(raw_file["id"]),
                        filename=str(raw_file["filename"]),
                        size=int(raw_file["size"]),
                        sha256=str(raw_file["sha256"]).lower(),
                        destination=str(raw_file["destination"]),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise InstantModelsError("Manifest zawiera nieprawidłowy plik") from exc
                if file.size <= 0 or len(file.sha256) != 64 or any(character not in "0123456789abcdef" for character in file.sha256):
                    raise InstantModelsError("Manifest zawiera nieprawidłowy rozmiar lub SHA-256")
                previous = seen_files.get(file.id)
                if previous and previous != file:
                    raise InstantModelsError("Manifest zawiera sprzeczne definicje pliku")
                destination_key = file.destination.replace("\\", "/").lower()
                owner = destinations.get(destination_key)
                if owner and owner != file.id:
                    raise InstantModelsError("Manifest przypisuje różne pliki do tej samej ścieżki")
                seen_files[file.id] = file
                destinations[destination_key] = file.id
                files.append(file)
            bundles.append(
                InstantBundle(
                    id=str(raw_bundle.get("id", "")),
                    title=str(raw_bundle.get("title", "")),
                    description=str(raw_bundle.get("description", "")),
                    total_size=sum(file.size for file in files),
                    files=tuple(files),
                )
            )
        subscription = payload.get("subscription", {})
        return InstantManifest(
            version=str(payload.get("version", "")),
            valid_until=str(subscription.get("validUntil", "")),
            bundles=tuple(bundle for bundle in bundles if bundle.id and bundle.title),
        )

    def download_url(self, file_id: str) -> str:
        payload = self._request("POST", f"files/{file_id}/download-url")
        url = payload.get("url")
        parsed = urlparse(url) if isinstance(url, str) else None
        allowed = [
            item.strip().lower()
            for item in os.getenv(
                "INSTANT_MODELS_DOWNLOAD_HOSTS", ".r2.cloudflarestorage.com"
            ).split(",")
            if item.strip()
        ]
        hostname = parsed.hostname.lower() if parsed and parsed.hostname else ""
        host_allowed = any(
            hostname == suffix.lstrip(".")
            or hostname.endswith(suffix if suffix.startswith(".") else f".{suffix}")
            for suffix in allowed
        )
        if not isinstance(url, str) or not parsed or parsed.scheme != "https" or not host_allowed:
            raise InstantModelsError("API nie zwróciło bezpiecznego adresu pobierania")
        return url


class InstantModelsManager:
    def __init__(
        self,
        state_dir: Path,
        api_url: str,
        client_factory: Callable[[str], InstantModelsClient] | None = None,
    ):
        self.store = StateStore(state_dir)
        self.credentials = CredentialsStore(state_dir)
        self.api_url = api_url
        self.client_factory = client_factory or (lambda token: InstantModelsClient(self.api_url, token))

    def _client(self) -> InstantModelsClient:
        token = self.credentials.load()
        if not token:
            raise InstantModelsError("Najpierw aktywuj Instant Models")
        return self.client_factory(token)

    @staticmethod
    def _bundle_summary(manifest: InstantManifest) -> list[dict[str, object]]:
        return [
            {
                "id": bundle.id,
                "title": bundle.title,
                "description": bundle.description,
                "total_size": bundle.total_size,
                "file_count": len(bundle.files),
            }
            for bundle in manifest.bundles
        ]

    def activate(self, token: str) -> dict[str, object]:
        token = token.strip()
        if not token.startswith("im_live_") or len(token) < 40:
            raise InstantModelsError("Token Instant Models ma nieprawidłowy format")
        manifest = self.client_factory(token).manifest()
        self.credentials.save(token)
        self.store.update(
            status="active",
            connected=True,
            token_masked=self.credentials.masked(),
            manifest_version=manifest.version,
            valid_until=manifest.valid_until,
            bundles=self._bundle_summary(manifest),
            model_count=len({file.id for bundle in manifest.bundles for file in bundle.files}),
            message="Instant Models aktywne. Pakiety z katalogu będą pobierane z R2.",
            error=None,
        )
        return self.status()

    def disconnect(self) -> dict[str, object]:
        self.credentials.delete()
        self.store.update(
            status="idle",
            connected=False,
            token_masked=None,
            bundles=[],
            valid_until=None,
            model_count=0,
            message="Instant Models rozłączone",
            error=None,
        )
        return self.status()

    def status(self) -> dict[str, object]:
        state = self.store.get()
        state["connected"] = bool(self.credentials.load())
        state["token_masked"] = self.credentials.masked()
        return state

    def source_for(self, workflow_id: str) -> InstantDownloadSource | None:
        if not self.credentials.load():
            return None
        client = self._client()
        manifest = client.manifest()
        bundle = next((item for item in manifest.bundles if item.id == workflow_id), None)
        if not bundle:
            raise InstantModelsError("Token nie ma dostępu do wybranego pakietu")
        self.store.update(
            status="active",
            connected=True,
            manifest_version=manifest.version,
            valid_until=manifest.valid_until,
            message="Instant Models aktywne. Pakiety z katalogu będą pobierane z R2.",
            error=None,
        )
        return InstantDownloadSource(bundle=bundle, client=client)
