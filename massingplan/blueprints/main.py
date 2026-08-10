"""The pages.

A project list, a project workspace, and the two things persistence unlocked:
setting a baseline, and seeing where the time went against it.
"""

from __future__ import annotations

from typing import Any

from flask import Blueprint, redirect, render_template, request, url_for

from ..api import errors, schedules
from ..models.identity import Permission
from ..services import accounts, projects
from ..services import repository as repo
from ..services.demo import demo_payload
from . import deps

bp = Blueprint("main", __name__)


@bp.get("/")
def index() -> Any:
    if deps.current_principal().is_authenticated:
        return redirect(url_for("main.projects_list"))
    return render_template("index.html")


@bp.get("/demo")
def demo() -> Any:
    """A worked schedule with no sign-in and nothing stored.

    Seeded from code rather than the database so the page works on a fresh
    clone with nothing migrated.
    """
    payload = demo_payload()
    return render_template(
        "workspace.html",
        title="Demo — mid-rise fragment",
        schedule=schedules.schedule_from_payload(payload),
        health=schedules.analyse(payload)["health"],
        project=None,
        baselines=[],
        comparison=None,
        issues=None,
    )


# -- projects --------------------------------------------------------------


@bp.get("/projects")
@deps.login_required
def projects_list() -> Any:
    rows = []
    for project in repo.list_projects(deps.db(), deps.current_org()):
        try:
            outcome = projects.reschedule(deps.db(), project)
            rows.append(projects.summary(project, outcome))
        except projects.ProjectError as exc:
            # An unschedulable project must still be listed and still be
            # deletable. Hiding it means the only way to fix it is the database.
            rows.append(
                {
                    "id": project.id,
                    "code": project.code,
                    "name": project.name,
                    "error": str(exc),
                    "slip_days": None,
                }
            )
    return render_template("projects.html", projects=rows)


@bp.get("/projects/<project_id>")
@deps.login_required
def project_detail(project_id: str) -> Any:
    project = deps.load_project(project_id)
    outcome = projects.reschedule(deps.db(), project)
    deps.db().commit()

    comparison = None
    baseline = project.current_baseline
    compare_to = request.args.get("compare")
    chosen = next((b for b in project.baselines if b.id == compare_to), baseline)
    if chosen is not None:
        comparison = projects.compare_to_baseline(deps.db(), project, chosen, outcome=outcome)

    return render_template(
        "workspace.html",
        title=f"{project.code} — {project.name}",
        schedule={**outcome.summary(), "activities": _rows_with_codes(project, outcome)},
        health=projects.assess(deps.db(), project, outcome),
        project=project,
        baselines=project.baselines,
        chosen_baseline=chosen,
        comparison=comparison,
        issues=None,
    )


def _rows_with_codes(project: Any, outcome: Any) -> list[dict[str, Any]]:
    """Rows labelled with the planner's code rather than the internal id.

    The id is correct and unrecognisable. Done at the presentation boundary, not
    in `to_rows()`, whose key set is frozen by test on purpose.
    """
    labels = {a.id: (a.code, a.name) for a in project.activities}
    out = []
    for row in outcome.to_rows():
        code, name = labels.get(str(row["activity_id"]), ("", ""))
        out.append({**row, "code": code or row["activity_id"], "name": name})
    return out


@bp.post("/projects/<project_id>/baseline")
@deps.require_permission(Permission.BASELINE_SET)
def set_baseline(project_id: str) -> Any:
    project = deps.load_project(project_id)
    name = request.form.get("name", "").strip() or "Baseline"
    notes = request.form.get("notes", "").strip()
    principal = deps.current_principal()
    try:
        with deps.committing() as session:
            baseline = projects.set_baseline(session, project, name=name, notes=notes)
            accounts.audit(
                session,
                organization_id=project.organization_id,
                action="baseline.set",
                actor_id=principal.subject_id,
                actor_label=principal.label,
                subject_type="project",
                subject_id=project.id,
                summary=f"set baseline {name!r}",
                detail={"baseline_id": baseline.id, "finish": str(baseline.project_finish)},
            )
    except projects.ProjectError as exc:
        return render_template(
            "error.html", error=type("E", (), {"message": str(exc), "detail": None})()
        ), 400
    return redirect(url_for("main.project_detail", project_id=project.id))


@bp.post("/projects/<project_id>/delete")
@deps.require_permission(Permission.PROJECT_DELETE)
def delete_project(project_id: str) -> Any:
    project = deps.load_project(project_id)
    principal = deps.current_principal()
    with deps.committing() as session:
        accounts.audit(
            session,
            organization_id=project.organization_id,
            action="project.delete",
            actor_id=principal.subject_id,
            actor_label=principal.label,
            subject_type="project",
            subject_id=project.id,
            # Recorded *before* the delete, and the summary carries the name --
            # after the row is gone, an audit entry holding only an id is a
            # reference to nothing.
            summary=f"deleted project {project.code} ({project.name})",
        )
        session.delete(project)
    return redirect(url_for("main.projects_list"))


# -- import ----------------------------------------------------------------


@bp.route("/upload", methods=["GET", "POST"])
@deps.login_required
def upload() -> Any:
    if request.method == "GET":
        return render_template("upload.html", error=None)

    principal = deps.current_principal()
    if not principal.can(Permission.IMPORT_RUN):
        return render_template("upload.html", error="You do not have permission to import."), 403

    upload_file = request.files.get("file")
    if upload_file is None or not upload_file.filename:
        return render_template("upload.html", error="Choose a file first."), 400

    raw = upload_file.read()
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        # P6 exports are frequently Windows-1252. Replacing an undecodable byte
        # loses an accent; refusing loses the whole schedule.
        text = raw.decode("cp1252", errors="replace")

    from ..core.mspdi import MSPDIError, read_mspdi
    from ..core.xer import XERError, read_xer

    try:
        stripped = text.lstrip()
        schedule = read_mspdi(text) if stripped.startswith("<") else read_xer(text)
    except (XERError, MSPDIError) as exc:
        return render_template("upload.html", error=f"Could not read that file: {exc}"), 415

    name = schedule.project_name or upload_file.filename
    try:
        with deps.committing() as session:
            project, _outcome, job = projects.import_schedule(
                session,
                schedule,
                organization_id=principal.organization_id or "",
                name=name,
                filename=upload_file.filename,
            )
            accounts.audit(
                session,
                organization_id=project.organization_id,
                action="project.import",
                actor_id=principal.subject_id,
                actor_label=principal.label,
                subject_type="project",
                subject_id=project.id,
                summary=f"imported {upload_file.filename} ({job.activity_count} activities)",
                detail={"has_logic": job.has_logic, "format": job.source_format},
            )
            project_id = project.id
    except projects.ProjectError as exc:
        return render_template("upload.html", error=str(exc)), 422

    return redirect(url_for("main.project_detail", project_id=project_id))


@bp.get("/projects/<project_id>/export.xer")
@deps.login_required
def export_xer(project_id: str) -> Any:
    from flask import Response

    from ..core.xer import write_xer

    project = deps.load_project(project_id)
    body = write_xer(repo.to_exchange(project))
    return Response(
        body,
        mimetype="text/plain",
        headers={"Content-Disposition": f'attachment; filename="{project.code}.xer"'},
    )


# -- account ---------------------------------------------------------------


@bp.get("/account")
@deps.login_required
def account() -> Any:
    principal = deps.current_principal()
    from sqlalchemy import select

    from ..models import ApiKey, AuditEvent, User

    session = deps.db()
    user = session.get(User, principal.subject_id)
    keys = session.scalars(
        select(ApiKey)
        .where(ApiKey.organization_id == principal.organization_id)
        .order_by(ApiKey.created_at.desc())
    ).all()
    events = session.scalars(
        select(AuditEvent)
        .where(AuditEvent.organization_id == principal.organization_id)
        .order_by(AuditEvent.at.desc())
        .limit(50)
    ).all()
    return render_template(
        "account.html",
        principal=principal,
        organizations=accounts.organizations_for(session, user) if user else [],
        keys=keys,
        events=events,
        issued_key=request.args.get("issued", ""),
    )


@bp.post("/account/keys")
@deps.require_permission(Permission.KEY_MANAGE)
def create_key() -> Any:
    principal = deps.current_principal()
    from ..models import User

    name = request.form.get("name", "").strip() or "API key"
    with deps.committing() as session:
        user = session.get(User, principal.subject_id)
        plaintext, record = accounts.issue_api_key(
            session,
            organization_id=principal.organization_id or "",
            name=name,
            created_by=user,
        )
        accounts.audit(
            session,
            organization_id=principal.organization_id or "",
            action="key.issue",
            actor_id=principal.subject_id,
            actor_label=principal.label,
            subject_type="api_key",
            subject_id=record.id,
            summary=f"issued API key {record.prefix}...",
        )
    # Shown once, in the URL of a redirect the user already has. Storing it to
    # show later would defeat hashing it at rest.
    return redirect(url_for("main.account", issued=plaintext))


@bp.post("/account/keys/<key_id>/revoke")
@deps.require_permission(Permission.KEY_MANAGE)
def revoke_key(key_id: str) -> Any:
    from ..models import ApiKey

    principal = deps.current_principal()
    with deps.committing() as session:
        record = session.get(ApiKey, key_id)
        if record is None or record.organization_id != principal.organization_id:
            raise errors.NotFound("no such key")
        accounts.revoke_api_key(session, record)
        accounts.audit(
            session,
            organization_id=principal.organization_id or "",
            action="key.revoke",
            actor_id=principal.subject_id,
            actor_label=principal.label,
            subject_type="api_key",
            subject_id=record.id,
            summary=f"revoked API key {record.prefix}...",
        )
    return redirect(url_for("main.account"))
