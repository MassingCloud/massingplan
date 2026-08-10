"""The default: a directory on this machine.

Two things it refuses to do, both of which are the same bug in different
clothes: escape its root, and hand back a URL.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from .base import StorageBackend, StoragePointer


class LocalStorage(StorageBackend):
    name = "local"

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, key: str) -> Path:
        # Resolve, then check containment. A key of `../../etc/passwd` is a
        # traversal, and string-prefix checks on the unresolved path miss it.
        candidate = (self.root / key).resolve()
        if not candidate.is_relative_to(self.root):
            raise ValueError(f"storage key {key!r} escapes the storage root")
        return candidate

    def put(
        self, key: str, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> StoragePointer:
        path = self._path(key)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return StoragePointer(
            backend=self.name,
            key=key,
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            content_type=content_type,
        )

    def get(self, pointer: StoragePointer) -> bytes:
        data = self._path(pointer.key).read_bytes()
        # The digest is checked on read, not just recorded on write. Silent
        # corruption in a stored schedule would surface as a parse error weeks
        # later, pointing at the parser.
        if hashlib.sha256(data).hexdigest() != pointer.sha256:
            raise OSError(f"stored object {pointer.key!r} does not match its recorded digest")
        return data

    def delete(self, pointer: StoragePointer) -> None:
        self._path(pointer.key).unlink(missing_ok=True)
