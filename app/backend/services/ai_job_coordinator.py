from __future__ import annotations

import os
import threading
from pathlib import Path


class RepositoryAiJobCoordinator:
    """Process-local exclusion shared by every AI proposal service."""

    _lock = threading.Lock()
    _active: set[str] = set()

    @classmethod
    def key(cls, repository: str | Path) -> str:
        return os.path.normcase(str(Path(repository).resolve()))

    @classmethod
    def claim(cls, repository: str | Path) -> bool:
        key = cls.key(repository)
        with cls._lock:
            if key in cls._active:
                return False
            cls._active.add(key)
            return True

    @classmethod
    def release(cls, repository: str | Path) -> None:
        with cls._lock:
            cls._active.discard(cls.key(repository))
