"""Storage adapters, resolved by name."""

from __future__ import annotations

from typing import Any

from ..entitlement.base import AdapterUnavailableError
from .base import StorageBackend, StoragePointer
from .local import LocalStorage

__all__ = [
    "AdapterUnavailableError",
    "LocalStorage",
    "StorageBackend",
    "StoragePointer",
    "resolve",
]


def resolve(backend: str, **kwargs: Any) -> StorageBackend:
    if backend == "local":
        return LocalStorage(**kwargs)
    if backend == "s3":
        try:
            from .s3 import S3Storage
        except ImportError as exc:
            raise AdapterUnavailableError(
                "The S3 storage adapter is not installed. "
                "Install it with: pip install 'massingplan[s3]'"
            ) from exc
        return S3Storage(**kwargs)
    raise AdapterUnavailableError(f"unknown storage backend {backend!r}; expected 'local' or 's3'")
