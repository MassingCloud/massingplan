"""The pages.

A project list, a project workspace, and the two things persistence unlocked:
setting a baseline, and seeing where the time went against it.
"""

from __future__ import annotations

import math
from typing import Any

from flask import Blueprint, redirect, render_template, request, url_for

from ..api import errors, schedules
from ..models.identity import Permission
from ..models.webhooks import WebhookEvent
from ..services import accounts, mfa, projects, webhooks
from ..services import repository as repo
from ..services.demo import demo_payload
from . import deps

bp = Blueprint("main", __name__)

#: An enrolment in flight. Held in the session, not the database: a secret
#: written before the user proves they can produce a code from it leaves an
#: account demanding a factor nobody has.
MFA_PENDING_SECRET = "mfa_pending_secret"  # noqa: S105 - a session key, not a secret
MFA_PENDING_RECOVERY = "mfa_pending_recovery"


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


@bp.get("/linear")
def linear() -> Any:
    """A worked location-based schedule, with no sign-in and nothing stored.

    Seeded from code like `/demo`, and for the same reason: so the feature can
    be seen without an account. A *real* project keeps its breakdown in the
    database now -- `/projects/<id>/linear`. This docstring said locations were
    not persisted until they were, which is the class of stale claim this repo
    has already had to walk back twice.
    """
    from ..services.demo import linear_demo_payload

    payload = linear_demo_payload()
    return render_template(
        "linear.html",
        title="Line of balance — eight floors, five trades",
        linear=schedules.schedule_linear(payload),
        trades=payload["tasks"],
        locations=payload["locations"],
    )


# -- projects --------------------------------------------------------------


@bp.get("/projects")
@deps.login_required
def projects_list() -> Any:
    # Read from the denormalised columns. Rescheduling to render a list is a
    # full forward and backward pass plus a write-back *per row*, so fifty
    # projects is fifty CPM runs and fifty transactions to draw a table nobody
    # has clicked into -- and it degrades linearly with the thing a growing
    # customer has more of. The columns are written on import and on every visit
    # to a project page; `stale` covers the gap for one that has never been
    # opened.
    rows = [
        projects.stored_summary(project)
        for project in repo.list_projects(deps.db(), deps.current_org())
    ]
    return render_template("projects.html", projects=rows)


@bp.get("/projects/<project_id>")
@deps.login_required
def project_detail(project_id: str) -> Any:
    project = deps.load_project(project_id)
    # One network build for the page. `reschedule` used to be followed by a
    # second `repo.to_network()` inside `_rows_with_codes`, converting every
    # activity and relationship twice per view.
    outcome, links = projects.reschedule_with_links(deps.db(), project)
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
        schedule={**outcome.summary(), "activities": _rows_with_codes(project, outcome, links)},
        health=projects.assess(deps.db(), project, outcome),
        project=project,
        baselines=project.baselines,
        chosen_baseline=chosen,
        comparison=comparison,
        issues=None,
    )


def _rows_with_codes(project: Any, outcome: Any, links: list[Any]) -> list[dict[str, Any]]:
    """Rows labelled with the planner's code, and carrying their predecessors.

    Both come from `schedules.chart_rows`, which is the one place that decorates
    a row for display. This function used to do half of it inline and omit the
    relationships entirely, which is why the Gantt drew no dependency arrows.

    `links` is passed in rather than rebuilt: the caller already has them from
    the schedule run.
    """
    return schedules.chart_rows(
        outcome,
        links,
        {a.id: (a.code, a.name) for a in project.activities},
        {a.id: a.kind.value for a in project.activities},
    )


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
            # Queued in the same transaction as the baseline. An event that
            # commits separately is an event about something that may not have
            # happened.
            webhooks.emit(
                session,
                organization_id=project.organization_id,
                event=WebhookEvent.BASELINE_SET,
                payload={
                    "project_id": project.id,
                    "project_code": project.code,
                    "baseline_id": baseline.id,
                    "baseline_name": name,
                    "project_finish": str(baseline.project_finish),
                },
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
        # Emitted before the delete, and carrying the code and name. After the
        # row is gone, a payload holding only an id is a reference to nothing --
        # the subscriber cannot look it up, because that is the point.
        webhooks.emit(
            session,
            organization_id=project.organization_id,
            event=WebhookEvent.PROJECT_DELETED,
            payload={"project_id": project.id, "project_code": project.code, "name": project.name},
        )
        session.delete(project)
    return redirect(url_for("main.projects_list"))


# -- the location model ----------------------------------------------------


@bp.get("/projects/<project_id>/linear")
@deps.login_required
def project_linear(project_id: str) -> Any:
    """The project's line of balance, or the form to define one."""
    project = deps.load_project(project_id)
    try:
        linear = projects.linear_schedule(project)
    except projects.ProjectError as exc:
        return render_template(
            "project_linear.html",
            project=project,
            linear=None,
            error=str(exc),
            submitted=None,
        ), 400
    return render_template(
        "project_linear.html", project=project, linear=linear, error=None, submitted=None
    )


@bp.get("/projects/<project_id>/takt")
@deps.login_required
def project_takt(project_id: str) -> Any:
    """The same location model, planned as a takt train.

    Deliberately the same breakdown and the same take-off as the line of
    balance next door: the two methods disagree about what to do with the work,
    not about what the work is. Putting them side by side on one model is the
    only honest way to choose between them -- one shows what continuity costs,
    the other what the rhythm costs.

    `?takt_days=` overrides the rhythm; omitted, the page uses the shortest
    feasible one and says which trade sets it.
    """
    project = deps.load_project(project_id)
    raw = request.args.get("takt_days", "").strip()
    try:
        wanted = int(raw) if raw else None
    except ValueError:
        wanted = None
    try:
        takt = projects.takt_plan(project, takt_days=wanted)
    except (projects.ProjectError, ValueError) as exc:
        return render_template("project_takt.html", project=project, takt=None, error=str(exc)), 400
    return render_template("project_takt.html", project=project, takt=takt, error=None)


@bp.post("/projects/<project_id>/linear/locations")
@deps.require_permission(Permission.PROJECT_WRITE)
def set_locations(project_id: str) -> Any:
    """Replace the whole breakdown, one location per line.

    A textarea rather than a row-at-a-time editor: a breakdown is forty floors
    entered once, and making somebody click "add" forty times is how a feature
    goes unused. `Key | Name` on each line, the name optional.
    """
    project = deps.load_project(project_id)
    raw = request.form.get("locations", "")
    lines = raw.splitlines()
    # Bounded before the loop, not after: `replace_locations` inserts a row per
    # entry inside one transaction, so an unbounded textarea is an unbounded
    # write. A 16 MB body of `L\n` is eight million inserts.
    if len(lines) > projects.MAX_BREAKDOWN_LINES:
        return _linear_error(
            project,
            f"That is {len(lines)} lines. A breakdown is one line per place in "
            f"the building; this refuses more than {projects.MAX_BREAKDOWN_LINES}.",
        )

    entries = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        key, _, name = line.partition("|")
        key, name = key.strip()[:80], name.strip()[:200]
        if not key:
            return _linear_error(
                project,
                "A location needs a key -- that is what the take-off and the "
                "trades reference. A line starting with `|` has none.",
            )
        entries.append((key, name))

    seen = {key for key, _ in entries}
    if len(seen) != len(entries):
        return _linear_error(
            project,
            "Two locations share a key. Each one has to be distinct -- the trades reference it.",
        )

    with deps.committing() as session:
        repo.replace_locations(session, project, entries)
        accounts.audit(
            session,
            organization_id=project.organization_id,
            action="linear.locations",
            actor_id=deps.current_principal().subject_id,
            actor_label=deps.current_principal().label,
            subject_type="project",
            subject_id=project.id,
            summary=f"set a {len(entries)}-location breakdown",
        )
    return redirect(url_for("main.project_linear", project_id=project.id))


@bp.post("/projects/<project_id>/linear/trades")
@deps.require_permission(Permission.PROJECT_WRITE)
def add_trade(project_id: str) -> Any:
    project = deps.load_project(project_id)
    key = request.form.get("key", "").strip()[:80]
    if not key:
        return _linear_error(
            project, "A trade needs a key -- it is what the handover order refers to."
        )

    def _int(field: str, default: int) -> int:
        try:
            return int(request.form.get(field) or default)
        except ValueError:
            return default

    rate_raw = request.form.get("rate", "").strip()
    try:
        rate = float(rate_raw) if rate_raw else None
    except ValueError:
        return _linear_error(project, f"{rate_raw!r} is not a production rate.")
    # `not (rate > 0)` rather than `rate <= 0`, because `float("nan")` passes
    # every ordered comparison and then raises inside `math.ceil` in the engine.
    # Infinity is finite-checked for the same reason: it divides to zero days.
    if rate is not None and not (rate > 0 and math.isfinite(rate)):
        return _linear_error(
            project,
            f"{rate_raw!r} is not a production rate. It has to be a positive "
            "number, or blank to use the flat duration instead.",
        )

    # An empty box leaves the stored take-off alone rather than wiping it: the
    # form is used to correct a buffer or a name far more often than to remove
    # quantities, and clearing them by omission would be a silent loss. To stop
    # using a take-off, clear the *rate* -- the flat duration then applies.
    quantities_raw = request.form.get("quantities", "").strip()
    quantities = None
    if quantities_raw:
        quantities, problems = projects.parse_quantities(
            quantities_raw, [loc.key for loc in project.locations]
        )
        if problems:
            return _linear_error(project, "The take-off could not be read: " + _listed(problems))
        # `quantities_raw`, not `quantities`: the condition is what the planner
        # typed, not what survived parsing. Keying it on the parsed dict means
        # any future path that returns an empty one skips this check and stores
        # a take-off nobody asked for -- which is how the empty-breakdown case
        # got through in the first place.
        if rate is None:
            return _linear_error(
                project,
                "Quantities need a production rate to become durations. Add a "
                "rate, or remove the quantities and give a flat duration.",
            )
        if not quantities:
            return _linear_error(
                project,
                "That take-off produced no quantities. Nothing was stored -- "
                "check the location names against the breakdown above.",
            )

    with deps.committing() as session:
        repo.upsert_linear_activity(
            session,
            project,
            key=key,
            name=request.form.get("name", "").strip()[:200],
            duration_days=max(0, _int("duration_days", 1)),
            rate=rate,
            buffer_days=_int("buffer_days", 0),
            crews=max(1, _int("crews", 1)),
            quantities=quantities,
        )
    return redirect(url_for("main.project_linear", project_id=project.id))


def _listed(problems: list[str], *, limit: int = 10) -> str:
    """Join problems for display, saying so when there are more than fit.

    A capped list that does not name its cap reads as the complete list, and
    the planner fixes ten lines, resubmits, and is told about ten more.
    """
    shown = "; ".join(problems[:limit])
    if len(problems) <= limit:
        return shown
    return f"{shown} -- and {len(problems) - limit} more"


def _linear_error(project: Any, message: str) -> Any:
    """Re-render the location page with a reason, rather than a bare 400.

    Two halves, and the second is the one that gets forgotten: the message says
    which line was wrong, and `submitted` hands the form back what was typed.
    Naming line 37 of a forty-line take-off and then clearing the box is not
    materially better than not naming it.
    """
    try:
        linear = projects.linear_schedule(project)
    except projects.ProjectError:
        linear = None
    return render_template(
        "project_linear.html",
        project=project,
        linear=linear,
        error=message,
        submitted=request.form,
    ), 400


@bp.post("/projects/<project_id>/linear/trades/<trade_key>/delete")
@deps.require_permission(Permission.PROJECT_WRITE)
def delete_trade(project_id: str, trade_key: str) -> Any:
    project = deps.load_project(project_id)
    with deps.committing() as session:
        trade = next((a for a in project.linear_activities if a.key == trade_key), None)
        if trade is None:
            raise errors.NotFound("no such trade")
        project.linear_activities.remove(trade)
        session.flush()
    return redirect(url_for("main.project_linear", project_id=project.id))


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

    # Read it and close it. Werkzeug spools a multipart part to a temp file, and
    # leaving the handle to the garbage collector means an unraisable
    # ResourceWarning at an arbitrary later moment and a temp file held open for
    # as long as the object lives. On a busy importer that is a file descriptor
    # leak; here it was the thing that surfaced when warnings became errors.
    filename = upload_file.filename
    try:
        raw = upload_file.read()
    finally:
        upload_file.close()
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

    name = schedule.project_name or filename
    try:
        with deps.committing() as session:
            project, _outcome, job = projects.import_schedule(
                session,
                schedule,
                organization_id=principal.organization_id or "",
                name=name,
                filename=filename,
            )
            accounts.audit(
                session,
                organization_id=project.organization_id,
                action="project.import",
                actor_id=principal.subject_id,
                actor_label=principal.label,
                subject_type="project",
                subject_id=project.id,
                summary=f"imported {filename} ({job.activity_count} activities)",
                detail={"has_logic": job.has_logic, "format": job.source_format},
            )
            webhooks.emit(
                session,
                organization_id=project.organization_id,
                event=WebhookEvent.IMPORT_COMPLETED,
                payload={
                    "project_id": project.id,
                    "project_code": project.code,
                    "filename": filename,
                    "activity_count": job.activity_count,
                    # Carried because it is the one thing a subscriber must act
                    # on: a schedule with no logic reads as entirely critical.
                    "has_logic": job.has_logic,
                    "format": job.source_format,
                },
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
        mfa_enabled=bool(user and mfa.is_enabled(user)),
        mfa_available=mfa.is_available(),
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


# -- webhooks --------------------------------------------------------------


@bp.get("/account/webhooks")
@deps.require_permission(Permission.WEBHOOK_MANAGE)
def webhooks_page() -> Any:
    from sqlalchemy import select

    from ..models import Webhook, WebhookDelivery

    session = deps.db()
    org = deps.current_org()
    hooks = session.scalars(
        select(Webhook).where(Webhook.organization_id == org).order_by(Webhook.created_at.desc())
    ).all()
    recent = session.scalars(
        select(WebhookDelivery)
        .where(WebhookDelivery.organization_id == org)
        .order_by(WebhookDelivery.created_at.desc())
        .limit(25)
    ).all()
    return render_template(
        "account_webhooks.html",
        hooks=hooks,
        deliveries=recent,
        events=[e.value for e in WebhookEvent],
        issued_secret=request.args.get("secret", ""),
        error=None,
    )


@bp.post("/account/webhooks")
@deps.require_permission(Permission.WEBHOOK_MANAGE)
def create_webhook() -> Any:
    principal = deps.current_principal()
    url = request.form.get("url", "").strip()
    chosen = request.form.getlist("events")
    name = request.form.get("name", "").strip()

    try:
        with deps.committing() as session:
            hook, secret = webhooks.subscribe(
                session,
                organization_id=principal.organization_id or "",
                url=url,
                events=chosen,
                name=name,
                created_by_id=principal.subject_id,
                require_tls=_require_tls(),
            )
            accounts.audit(
                session,
                organization_id=principal.organization_id or "",
                action="webhook.create",
                actor_id=principal.subject_id,
                actor_label=principal.label,
                subject_type="webhook",
                subject_id=hook.id,
                # The URL, never the secret. An audit row carrying the
                # credential whose issue it records is a second copy of it.
                summary=f"subscribed {hook.url} to {', '.join(hook.events)}",
            )
    except webhooks.WebhookError as exc:
        from sqlalchemy import select

        from ..models import Webhook

        hooks = (
            deps.db()
            .scalars(select(Webhook).where(Webhook.organization_id == principal.organization_id))
            .all()
        )
        return render_template(
            "account_webhooks.html",
            hooks=hooks,
            deliveries=[],
            events=[e.value for e in WebhookEvent],
            issued_secret="",
            error=str(exc),
        ), 400

    # Shown once, like an API key. Storing it to show later would defeat
    # encrypting it at rest.
    return redirect(url_for("main.webhooks_page", secret=secret))


@bp.post("/account/webhooks/<webhook_id>/delete")
@deps.require_permission(Permission.WEBHOOK_MANAGE)
def delete_webhook(webhook_id: str) -> Any:
    from ..models import Webhook

    principal = deps.current_principal()
    with deps.committing() as session:
        hook = session.get(Webhook, webhook_id)
        if hook is None or hook.organization_id != principal.organization_id:
            raise errors.NotFound("no such webhook")
        accounts.audit(
            session,
            organization_id=principal.organization_id or "",
            action="webhook.delete",
            actor_id=principal.subject_id,
            actor_label=principal.label,
            subject_type="webhook",
            subject_id=hook.id,
            summary=f"removed the webhook to {hook.url}",
        )
        session.delete(hook)
    return redirect(url_for("main.webhooks_page"))


def _require_tls() -> bool:
    """Plain http only where the operator is plainly developing.

    An unencrypted webhook publishes every event, and its signature, to anyone
    on the path -- so the exception is narrow and tied to the environment rather
    than to a checkbox somebody can tick in production.
    """
    from flask import current_app

    settings = current_app.extensions.get("massingplan_settings")
    return getattr(settings, "env", "production") == "production"


@bp.get("/account/mfa")
@deps.login_required
def mfa_setup() -> Any:
    """Show the QR and the recovery codes. Nothing is stored yet.

    The secret is parked in the session for this one step rather than written to
    the database, because a half-finished enrolment would otherwise leave an
    account demanding a factor nobody can produce, fixable only by an
    administrator.
    """
    from ..models import User

    principal = deps.current_principal()
    user = deps.db().get(User, principal.subject_id)
    if user is None:
        raise errors.NotFound("no such account")

    if mfa.is_enabled(user):
        return render_template(
            "account_mfa.html",
            enabled=True,
            enrolment=None,
            display_secret="",
            remaining=mfa.remaining_recovery_codes(user),
            error=None,
        )
    if not mfa.is_available():
        return render_template(
            "account_mfa.html",
            enabled=False,
            enrolment=None,
            display_secret="",
            remaining=0,
            error=(
                "Two-factor authentication is not available on this install. It "
                "needs `pip install 'massingplan[mfa]'` and a "
                "MASSINGPLAN_ENCRYPTION_KEY."
            ),
        )

    enrolment = mfa.begin_enrolment(user)
    session_store = deps.web_session()
    session_store[MFA_PENDING_SECRET] = enrolment.secret
    session_store[MFA_PENDING_RECOVERY] = enrolment.recovery_codes
    return render_template(
        "account_mfa.html",
        enabled=False,
        enrolment=enrolment,
        display_secret=mfa.secret_for_display(enrolment.secret),
        remaining=0,
        error=None,
    )


@bp.post("/account/mfa")
@deps.login_required
def mfa_enable() -> Any:
    from ..models import User

    principal = deps.current_principal()
    session_store = deps.web_session()
    secret = session_store.get(MFA_PENDING_SECRET)
    recovery = list(session_store.get(MFA_PENDING_RECOVERY) or [])
    if not secret:
        # The enrolment expired or the session was cleared. Start again rather
        # than storing a secret the user's phone may no longer hold.
        return redirect(url_for("main.mfa_setup"))

    try:
        with deps.committing() as session:
            user = session.get(User, principal.subject_id)
            if user is None:
                raise errors.NotFound("no such account")
            mfa.confirm_enrolment(
                session,
                user,
                secret=secret,
                code=request.form.get("code", ""),
                recovery_codes=recovery,
            )
            accounts.audit(
                session,
                organization_id=principal.organization_id or "",
                action="mfa.enable",
                actor_id=principal.subject_id,
                actor_label=principal.label,
                subject_type="user",
                subject_id=user.id,
                summary="enabled two-factor authentication",
            )
    except mfa.MfaError as exc:
        # Re-render with the *same* secret and codes. Issuing a new pair here
        # would invalidate the QR the user has already scanned, so a mistyped
        # digit would mean starting over.
        return (
            render_template(
                "account_mfa.html",
                enabled=False,
                enrolment=mfa.Enrolment(secret=secret, uri="", qr_svg="", recovery_codes=recovery),
                display_secret=mfa.secret_for_display(secret),
                remaining=0,
                error=str(exc),
            ),
            400,
        )

    session_store.pop(MFA_PENDING_SECRET, None)
    session_store.pop(MFA_PENDING_RECOVERY, None)
    return redirect(url_for("main.account"))


@bp.post("/account/mfa/disable")
@deps.login_required
def mfa_disable() -> Any:
    """Turning it off needs the current password.

    Otherwise a borrowed session strips the factor that session was supposed to
    be protected by, which is the one moment the factor exists for.
    """
    from ..models import User

    principal = deps.current_principal()
    with deps.committing() as session:
        user = session.get(User, principal.subject_id)
        if user is None:
            raise errors.NotFound("no such account")
        if not accounts.verify_password(request.form.get("password", ""), user.password_hash):
            return (
                render_template(
                    "account_mfa.html",
                    enabled=True,
                    enrolment=None,
                    display_secret="",
                    remaining=mfa.remaining_recovery_codes(user),
                    error="That password did not match.",
                ),
                401,
            )
        mfa.disable(session, user)
        accounts.audit(
            session,
            organization_id=principal.organization_id or "",
            action="mfa.disable",
            actor_id=principal.subject_id,
            actor_label=principal.label,
            subject_type="user",
            subject_id=user.id,
            summary="disabled two-factor authentication",
        )
    return redirect(url_for("main.account"))


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
