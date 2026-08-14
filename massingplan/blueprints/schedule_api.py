"""The JSON API. A thin translation over `massingplan.api`.

Every view is: read the request, call one `api` function, return its dict. No
domain logic here -- that is what makes the same surface mountable under FastAPI
in massing without a rewrite.

The namespace, the bearer-token header and the error envelope match
massing.cloud's convention (SPEC.md 3.2), so a client can speak to either
product without a second error table.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, jsonify, request

from ..api import errors, schedules
from ..models.identity import Permission
from . import deps

bp = Blueprint("schedule_api", __name__)


@bp.before_request
def require_a_credential() -> Any:
    """Every endpoint here needs a key, except the capability listing.

    A guard on the blueprint rather than a decorator per view: a decorator that
    has to be remembered is a decorator that will be forgotten on the next
    endpoint somebody adds, and the forgetting is silent.
    """
    from flask import jsonify

    if request.endpoint and request.endpoint.endswith("capabilities"):
        return None
    principal = deps.current_principal()
    # A key, and *only* a key. This blueprint is exempt from CSRF, and that
    # exemption is only sound because a bearer key is never sent ambiently by a
    # browser. Accepting a session cookie here would make every endpoint below a
    # CSRF-exempt, state-changing surface authenticated by a credential the
    # browser attaches on its own -- with nothing but the required JSON content
    # type between a malicious page and somebody's project list.
    if principal.is_authenticated and principal.via != "api_key":
        return jsonify(
            {
                "error": {
                    "code": "unauthenticated",
                    "message": (
                        "this API does not accept a session cookie. Issue an API "
                        "key from your account page and present it as "
                        "`Authorization: Bearer mpln_...`"
                    ),
                }
            }
        ), 401
    if not principal.is_authenticated:
        return jsonify(
            {
                "error": {
                    "code": "unauthenticated",
                    "message": (
                        "present an API key as `Authorization: Bearer mpln_...` or in `X-Api-Key`"
                    ),
                }
            }
        ), 401
    if not principal.can(Permission.PROJECT_READ):
        return jsonify(
            {"error": {"code": "forbidden", "message": "this key cannot read projects"}}
        ), 403
    return None


def _payload() -> dict[str, Any]:
    """The JSON body, or a 400 that says what was wrong with it.

    A required JSON content type is also half the CSRF defence for this
    blueprint (see `app.py`): a cross-origin form post cannot set it.
    """
    if not request.is_json:
        raise errors.ApiError(
            "this endpoint takes application/json",
            detail=f"got Content-Type: {request.content_type or 'none'}",
        )
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        raise errors.ApiError("the request body must be a JSON object")
    return body


@bp.post("/schedule")
def schedule() -> Any:
    """Compute a schedule from a network."""
    return jsonify(schedules.schedule_from_payload(_payload()))


@bp.post("/analyse")
def analyse() -> Any:
    """Compute a schedule and score it against the DCMA 14 points."""
    return jsonify(schedules.analyse(_payload()))


@bp.post("/risk")
def risk() -> Any:
    """Monte Carlo the network. Seeded, so the same plan gives the same answer."""
    return jsonify(schedules.simulate_risk(_payload()))


@bp.post("/linear")
def linear() -> Any:
    """Location-based scheduling: the flow, the interferences, and the network."""
    return jsonify(schedules.schedule_linear(_payload()))


@bp.post("/takt")
def takt() -> Any:
    """Takt planning: the train, the crews it needs, and what the rhythm cost."""
    return jsonify(schedules.schedule_takt(_payload()))


@bp.post("/level")
def level() -> Any:
    """Resource-level the network. Advisory by default -- core never writes."""
    return jsonify(schedules.level_resources(_payload()))


@bp.post("/compare")
def compare() -> Any:
    """Diff two schedules and attribute the finish move."""
    return jsonify(schedules.compare_baselines(_payload()))


@bp.post("/windows")
def windows() -> Any:
    """Which period lost the time, across a series of contemporaneous updates."""
    return jsonify(schedules.analyse_windows(_payload()))


@bp.post("/import")
def import_schedule() -> Any:
    """Read an uploaded Primavera XER or MS Project MSPDI file."""
    upload = request.files.get("file")
    if upload is None:
        raise errors.ApiError(
            "no file uploaded", detail="post multipart/form-data with a `file` field"
        )
    raw = upload.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # P6 exports are frequently Windows-1252. Replacing undecodable bytes
        # loses an accent; refusing loses the whole schedule.
        text = raw.decode("cp1252", errors="replace")
    return jsonify(schedules.import_file(text, filename=upload.filename or ""))


@bp.get("/capabilities")
def capabilities() -> Any:
    """What this build can do, in the caller's terms.

    Named rather than discovered by trial: a client that has to POST a `.mpp` to
    find out it is unsupported has learned it the expensive way.
    """
    from ..core.mspdi import mpp_unavailable_reason

    return jsonify(
        {
            "formats": {
                "read": ["xer", "mspdi"],
                "write": ["xer", "mspdi"],
                "unsupported": {"mpp": mpp_unavailable_reason()},
            },
            "relationship_types": ["FS", "SS", "FF", "SF"],
            "constraint_types": [
                c.value
                for c in __import__(
                    "massingplan.core.constraints", fromlist=["ConstraintType"]
                ).ConstraintType
            ],
            "features": [
                "multi_calendar_cpm",
                "data_date_and_progressed_logic",
                "retained_logic_and_progress_override",
                "dcma_14_point",
                "monte_carlo_risk",
                "resource_levelling",
                "baseline_comparison_with_delay_attribution",
                "location_based_scheduling",
            ],
        }
    )
