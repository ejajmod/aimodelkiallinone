from __future__ import annotations

import json
import threading
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, state_dir: Path):
        self.state_dir = state_dir
        self.state_file = state_dir / "state.json"
        self._lock = threading.RLock()
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._state: dict[str, Any] = self._load()

    def _load(self) -> dict[str, Any]:
        try:
            data = json.loads(self.state_file.read_text(encoding="utf-8"))
            if data.get("status") in {"queued", "scanning", "downloading", "verifying", "installing", "restarting"}:
                data.update(
                    status="interrupted",
                    message="Kontener został zatrzymany podczas instalacji. Uruchom ją ponownie, aby wznowić pliki .part.",
                    updated_at=utc_now(),
                )
            return data
        except (OSError, json.JSONDecodeError):
            return {
                "status": "idle",
                "workflow_id": None,
                "progress": 0,
                "message": "Gotowy do instalacji",
                "updated_at": utc_now(),
            }

    def get(self) -> dict[str, Any]:
        with self._lock:
            return deepcopy(self._state)

    def update(self, **changes: Any) -> dict[str, Any]:
        with self._lock:
            self._state.update(changes, updated_at=utc_now())
            temporary = self.state_file.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(self._state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.state_file)
            return deepcopy(self._state)
