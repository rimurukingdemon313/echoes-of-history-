"""Filesystem storage.

Durable *only* if the directory is a mounted volume. The check is real rather
than assumed: on Railway a missing volume mount looks exactly like a working
directory until the next deploy silently discards it, so
:meth:`LocalStorage.durable` reports what it can actually determine and the
caller decides how much to trust it.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from ...errors import ConfigError, Permanent
from ...logging import get_logger

log = get_logger(__name__)


class LocalStorage:
    name = "local"

    def __init__(self, root: Path, *, assume_durable: bool | None = None) -> None:
        self._root = Path(root)
        try:
            self._root.mkdir(parents=True, exist_ok=True)
        except PermissionError as exc:
            # The usual cause is a mounted volume: it arrives owned by root
            # and replaces the directory the image created, so a container
            # that has dropped to a non-root user cannot write to it. A bare
            # "Errno 13" costs an operator an afternoon; naming the cause
            # costs one line.
            raise ConfigError(
                f"Cannot write to {self._root}: {exc.strerror}. This is "
                f"almost always a mounted volume owned by root while the "
                f"application runs unprivileged. Check that the volume's "
                f"mount path matches DATA_DIR ({self._root.parent}), and "
                f"that the image's entrypoint takes ownership of it at "
                f"startup."
            ) from exc
        # Writing is what actually matters, and a directory can exist while
        # being unwritable. Finding that out here beats finding it out
        # ninety minutes into a render.
        probe = self._root / ".write-test"
        try:
            probe.touch()
            probe.unlink()
        except OSError as exc:
            raise ConfigError(
                f"{self._root} exists but is not writable: {exc}. If this is "
                f"a mounted volume, its ownership does not match the user "
                f"this process runs as."
            ) from exc
        self._assume_durable = assume_durable

    def _path(self, key: str) -> Path:
        # A key containing '..' would escape the root. Refuse rather than
        # normalise: a key that shape is a bug upstream, not a path to fix.
        if key.startswith("/") or ".." in Path(key).parts:
            raise Permanent(f"unsafe storage key: {key!r}")
        return self._root / key

    def put(self, local_path: Path, key: str) -> str:
        dest = self._path(key)
        dest.parent.mkdir(parents=True, exist_ok=True)
        if Path(local_path).resolve() != dest.resolve():
            # Copy then rename, so a crash mid-copy cannot leave a truncated
            # file sitting at the final key looking complete.
            staging = dest.with_suffix(dest.suffix + ".partial")
            shutil.copy2(local_path, staging)
            os.replace(staging, dest)
        return key

    def get(self, key: str, dest: Path) -> Path:
        src = self._path(key)
        if not src.exists():
            raise Permanent(f"storage key not found: {key}")
        dest.parent.mkdir(parents=True, exist_ok=True)
        if src.resolve() != Path(dest).resolve():
            shutil.copy2(src, dest)
        return dest

    def exists(self, key: str) -> bool:
        try:
            return self._path(key).exists()
        except Permanent:
            return False

    def delete(self, key: str) -> None:
        path = self._path(key)
        if path.exists():
            path.unlink()

    def durable(self) -> bool:
        if self._assume_durable is not None:
            return self._assume_durable
        # A separate device from the image root is the signature of a mounted
        # volume. Not proof, but the only signal available from inside.
        try:
            return os.stat(self._root).st_dev != os.stat("/").st_dev
        except OSError:
            return False
