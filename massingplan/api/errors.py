"""Typed errors, mapped to HTTP by whichever web layer is mounted.

Status codes mirror the massing.cloud convention (SPEC.md 3.2) so a client can
talk to either product without a second error table.

The names have no ``Error`` suffix on purpose: they read as the API's own
vocabulary at the call site -- ``raise ValidationFailed(...)`` -- and every one
of them subclasses ``ApiError``, so the suffix would be noise repeated on each
line rather than information.
"""

from __future__ import annotations


class ApiError(Exception):
    """Something the caller asked for could not be done, and why."""

    status_code = 400
    code = "bad_request"

    def __init__(self, message: str, *, detail: object = None) -> None:
        super().__init__(message)
        self.message = message
        self.detail = detail

    def to_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"error": {"code": self.code, "message": self.message}}
        if self.detail is not None:
            payload["error"] = {**payload["error"], "detail": self.detail}  # type: ignore[dict-item]
        return payload


class ValidationFailed(ApiError):  # noqa: N818 - reads as the API's vocabulary
    """The payload was well formed but describes an unschedulable network.

    422 rather than 400: the request parsed, the *schedule* is the problem, and
    the two need different fixes.
    """

    status_code = 422
    code = "validation_failed"


class NotFound(ApiError):  # noqa: N818
    """404 -- and deliberately also what a cross-tenant read returns.

    "This exists but is not yours" tells one contractor that another
    contractor's project id is real.
    """

    status_code = 404
    code = "not_found"


class Unsupported(ApiError):  # noqa: N818
    """A format or capability this build does not provide."""

    status_code = 415
    code = "unsupported"
