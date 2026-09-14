from __future__ import annotations

import threading


class OperationBusy(RuntimeError):
    pass


class OperationCoordinator:
    """Serializes jobs that write into the shared ComfyUI tree."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._guard = threading.Lock()
        self._owner: str | None = None

    @property
    def owner(self) -> str | None:
        with self._guard:
            return self._owner

    def acquire(self, owner: str) -> None:
        if not self._lock.acquire(blocking=False):
            raise OperationBusy("Inna operacja na modelach jest już uruchomiona")
        with self._guard:
            self._owner = owner

    def release(self, owner: str) -> None:
        with self._guard:
            if self._owner != owner:
                return
            self._owner = None
            self._lock.release()
