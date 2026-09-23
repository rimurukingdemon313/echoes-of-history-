"""Durable storage for finished media.

The rule this exists to enforce: a container filesystem is scratch space. The
rendered video, the thumbnail and the research package must survive a
redeploy, because losing them means re-rendering an hour of video to recover
something that was already correct.

``put`` returns a storage key, never a path. Callers store the key.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol


class StorageProvider(Protocol):
    name: str

    def put(self, local_path: Path, key: str) -> str:
        """Store ``local_path`` under ``key``; return the durable key."""

    def get(self, key: str, dest: Path) -> Path:
        """Fetch ``key`` to ``dest``; return the local path."""

    def exists(self, key: str) -> bool:
        ...

    def delete(self, key: str) -> None:
        ...

    def durable(self) -> bool:
        """False if this storage does not survive a redeploy.

        The upload stage consults this: on non-durable storage it refuses to
        delete the local render until the upload has been confirmed.
        """
