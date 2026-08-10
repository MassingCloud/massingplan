"""The server-rendered pages.

Three of them: a demo schedule you can look at without setting anything up, an
upload page, and the workspace that renders the result. The workspace is the one
that matters -- it is the thing neither source system had. AIHackScheduler
computes the DCMA grade, the offenders and the P80 and surfaces none of it.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, render_template, request

from ..api import errors, schedules
from ..services.demo import demo_payload

bp = Blueprint("main", __name__)


@bp.get("/")
def index() -> Any:
    return render_template("index.html")


@bp.get("/demo")
def demo() -> Any:
    """A worked mid-rise fragment, scheduled and assessed on the spot.

    Seeded from code rather than a database so the page works on a fresh clone
    with nothing installed and nothing migrated.
    """
    payload = demo_payload()
    result = schedules.analyse(payload)
    schedule = schedules.schedule_from_payload(payload)
    return render_template(
        "workspace.html",
        title="Demo — mid-rise fragment",
        schedule=schedule,
        health=result["health"],
        source=None,
    )


@bp.route("/upload", methods=["GET", "POST"])
def upload() -> Any:
    if request.method == "GET":
        return render_template("upload.html", error=None)

    upload_file = request.files.get("file")
    if upload_file is None or not upload_file.filename:
        return render_template("upload.html", error="Choose a file first."), 400

    raw = upload_file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode("cp1252", errors="replace")

    try:
        imported = schedules.import_file(text, filename=upload_file.filename)
    except errors.ApiError as exc:
        return render_template("upload.html", error=exc.message), exc.status_code

    payload = {
        "activities": [
            {
                "id": row["activity_id"],
                "name": row["activity_id"],
                "duration_days": row["duration_days"],
            }
            for row in imported["activities"]
        ]
    }
    return render_template(
        "workspace.html",
        title=upload_file.filename,
        schedule=imported["schedule"] | {"activities": imported["activities"]},
        health=None,
        source=imported["source"],
        has_logic=imported["has_logic"],
        issues=imported["issues"],
        _payload=payload,
    )
