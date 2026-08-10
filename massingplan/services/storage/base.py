"""Where uploaded files and exports live.

``StoragePointer`` is opaque on purpose: it carries a backend name and a key,
never a URL. A pointer that is a URL is a capability, and one that leaks into a
log or an export is an unauthenticated download link with no expiry.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(frozen=True)
class StoragePointer:
    backend: str
    key: str
    size: int
    sha256: str
    content_type: str = "application/octet-stream"

    def to_dict(self) -> dict[str, object]:
        return {
            "backend": self.backend,
            "key": self.key,
            "size": self.size,
            "sha256": self.sha256,
            "content_type": self.content_type,
        }


class StorageBackend(ABC):
    name: str = "base"

    @abstractmethod
    def put(self, key: str, data: bytes, *, content_type: str = ...) -> StoragePointer: ...

    @abstractmethod
    def get(self, pointer: StoragePointer) -> bytes: ...

    @abstractmethod
    def delete(self, pointer: StoragePointer) -> None: ...
