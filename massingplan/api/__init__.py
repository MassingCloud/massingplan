"""The framework-agnostic surface.

Plain functions over primitives and JSON-safe dicts. **No Flask, no FastAPI, no
pydantic** -- enforced by an import-linter contract in `pyproject.toml`.

That constraint has one purpose: `massingplan/blueprints/` mounts these under
Flask, and `ibuilder/massing` can mount the same functions under FastAPI with a
fifteen-line adapter. An `api` module that imports `flask` makes the second
consumer impossible, and nothing would notice until somebody tried.

Errors are raised as :class:`ApiError`, which carries a status code and a stable
machine-readable code. Each web layer maps that to its own response type. The
status codes mirror massing.cloud's convention (SPEC.md 3.2) so a client can
speak to either product without a second error table.
"""

from .errors import ApiError, NotFound, ValidationFailed
from .schedules import (
    analyse,
    compare_baselines,
    import_file,
    level_resources,
    schedule_from_payload,
    simulate_risk,
)

__all__ = [
    "ApiError",
    "NotFound",
    "ValidationFailed",
    "analyse",
    "compare_baselines",
    "import_file",
    "level_resources",
    "schedule_from_payload",
    "simulate_risk",
]
