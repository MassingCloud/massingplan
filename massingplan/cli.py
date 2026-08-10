"""The ``massingplan`` command.

Plain ``argparse``: this is four subcommands, and a CLI framework would be a
dependency bought for nothing. Every subcommand returns an exit code, so
``massingplan check`` is usable as a container health probe and
``massingplan schedule`` as a build step.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _cmd_check(_args: argparse.Namespace) -> int:
    """Boot and report the resolved configuration.

    Prints what the process *actually* resolved, not what the file says. The
    failure this catches is an operator staring at a correct-looking `.env`
    while the container reads a different one.
    """
    from .config import Settings
    from .services import entitlement, identity, storage

    settings = Settings()
    try:
        secret = settings.resolve_secret_key()
    except RuntimeError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 1

    # Never the key itself. A short digest tail is enough to tell two keys
    # apart across a cluster -- which is the actual question an operator has --
    # and useless to anyone who reads the log.
    import hashlib

    fingerprint = hashlib.sha256(secret.encode()).hexdigest()[-6:]

    report = {
        "env": settings.env,
        "max_upload_bytes": settings.max_upload_bytes,
        "persistence": "none (stateless; schedules are computed per request)",
        "secret_key": f"set ({len(secret)} chars, sha ...{fingerprint})",
        "entitlement": entitlement.resolve("standalone").name,
        "identity": identity.resolve("local").name,
        "storage": storage.LocalStorage(Path("instance/storage")).name,
    }
    for key, value in report.items():
        print(f"{key:20} {value}")

    from datetime import date

    from .core.network import Task
    from .core.schedule import schedule_network

    outcome = schedule_network([Task("probe", "probe", 1)], data_date=date(2026, 1, 1))
    print(f"{'engine':20} ok (probe finishes {outcome.project_finish})")
    return 0


def _cmd_schedule(args: argparse.Namespace) -> int:
    """Schedule a JSON network or a Primavera/MS Project file, and print rows."""
    from .api import ApiError, import_file, schedule_from_payload

    text = Path(args.path).read_text(encoding="utf-8", errors="replace")
    try:
        if args.path.endswith(".json"):
            result = schedule_from_payload(json.loads(text))
        else:
            result = import_file(text, filename=args.path)
    except ApiError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        if exc.detail:
            print(json.dumps(exc.detail, indent=2, default=str), file=sys.stderr)
        return 1

    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0

    rows = result.get("activities", [])
    summary = result.get("schedule", result)
    print(
        f"{summary.get('project_start')} to {summary.get('project_finish')}  "
        f"({summary.get('duration_working_days')} working days, {len(rows)} activities)"
    )
    if result.get("has_logic") is False:
        print(
            "WARNING: no relationships were read. A network with no logic has no "
            "critical path -- every activity below reads as critical with zero float.",
            file=sys.stderr,
        )
    print(f"{'ACTIVITY':<24}{'START':<12}{'FINISH':<12}{'FLOAT':>7}")
    for row in rows:
        total = row["total_float_days"]
        print(
            f"{row.get('code') or row['activity_id']!s:<24}"
            f"{row['start']:<12}{row['finish']:<12}"
            f"{('—' if total is None else total):>7}"
        )
    return 0


def _cmd_assess(args: argparse.Namespace) -> int:
    """Run the DCMA 14-point assessment and print the grade."""
    from .api import ApiError, analyse

    try:
        result = analyse(json.loads(Path(args.path).read_text(encoding="utf-8")))
    except ApiError as exc:
        print(f"{exc.code}: {exc.message}", file=sys.stderr)
        return 1

    health = result["health"]
    print(
        f"Grade {health['grade']}  ({health['score']}% of {health['assessed']} runnable checks; "
        f"{health['skipped']} skipped, {health['failed']} failed)"
    )
    if not health["optimisable"]:
        print("NOT SAFE TO OPTIMISE: logic, float or CPLI failed.", file=sys.stderr)
    for check in health["checks"]:
        marker = {"pass": " ok ", "fail": "FAIL", "skipped": "skip"}[check["status"]]
        print(f"  [{marker}] {check['number']:>2}. {check['name']:<28} {check['detail']}")
    # Non-zero when a check failed, so this is usable as a build gate.
    return 1 if health["failed"] else 0


def _cmd_demo(args: argparse.Namespace) -> int:
    """Print the worked demo schedule, so the install proves itself."""
    from .api import analyse
    from .services.demo import demo_payload

    payload = demo_payload()
    if args.progressed:
        from .services.demo import demo_progressed_payload

        payload = demo_progressed_payload()
    print(json.dumps(analyse(payload), indent=2, default=str))
    return 0


def _cmd_init_db(_args: argparse.Namespace) -> int:
    """Run migrations and seed the default organisation."""
    from .config import Settings
    from .database import init_engine, session_scope
    from .services import repository as repo

    settings = Settings()
    init_engine(settings.database_url)
    from alembic import command
    from alembic.config import Config

    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", settings.database_url)
    command.upgrade(config, "head")
    with session_scope() as session:
        repo.ensure_default_organization(session)
    print(f"schema is at head on {settings.database_url}")
    return 0


def _cmd_create_admin(args: argparse.Namespace) -> int:
    """Create an owner. The password is read from the environment, never argv.

    A password passed as an argument is in the shell history, in `ps` output,
    and in the process list of every other user on the box.
    """
    import os

    from .config import Settings
    from .database import init_engine, session_scope
    from .models.identity import Role
    from .services import accounts
    from .services import repository as repo

    password = os.getenv("MASSINGPLAN_ADMIN_PASSWORD", "")
    if not password:
        print(
            "set MASSINGPLAN_ADMIN_PASSWORD in the environment. Passing a password "
            "as an argument leaves it in shell history and in `ps` output.",
            file=sys.stderr,
        )
        return 2

    settings = Settings()
    init_engine(settings.database_url)
    try:
        with session_scope() as session:
            org = repo.ensure_default_organization(session)
            user = accounts.register(
                session,
                email=args.email,
                password=password,
                display_name=args.name or args.email,
                organization_id=org.id,
                role=Role.OWNER,
            )
            accounts.audit(
                session,
                organization_id=org.id,
                action="user.create",
                actor_id=user.id,
                actor_label=user.email,
                summary="created an owner from the command line",
            )
    except accounts.AccountError as exc:
        print(f"could not create the account: {exc}", file=sys.stderr)
        return 1
    print(f"created owner {args.email}")
    return 0


def _cmd_seed_demo(_args: argparse.Namespace) -> int:
    """A signed-in account and the demo project, for a fresh install."""
    from .config import Settings
    from .database import init_engine, session_scope
    from .services import accounts, projects
    from .services import repository as repo

    settings = Settings()
    init_engine(settings.database_url)
    with session_scope() as session:
        org = repo.ensure_default_organization(session)
        created = accounts.bootstrap_demo_account(session, organization_id=org.id)
        if created is None:
            print("an account already exists; not seeding a demo one")
        else:
            user, password = created
            # Printed once, random, never a documented default. A published
            # default password is a published vulnerability the moment somebody
            # exposes the port.
            print(f"demo account: {user.email}")
            print(f"password:     {password}")

        from .api.schedules import exchange_from_payload  # noqa: F401
        from .services.demo import demo_payload

        payload = demo_payload()
        schedule = _schedule_from_demo(payload)
        if not repo.list_projects(session, org.id):
            projects.import_schedule(
                session, schedule, organization_id=org.id, name=payload["name"]
            )
            print("seeded the demo project")
    return 0


def _schedule_from_demo(payload: dict) -> Any:
    """Turn the demo payload into a hub-model schedule the store accepts."""
    from datetime import date

    from .core.constraints import parse as parse_constraint
    from .core.model import (
        Calendar,
        CalendarException,
        ExchangeActivity,
        ExchangeRelationship,
        ExchangeSchedule,
    )
    from .core.network import ActivityKind, RelationType

    def as_date(value: object) -> date | None:
        return date.fromisoformat(str(value)) if value else None

    schedule = ExchangeSchedule(
        project_name=payload["name"],
        data_date=as_date(payload.get("data_date")),
        planned_start=as_date(payload.get("data_date")),
        source_format="demo",
    )
    for index, cal in enumerate(payload["calendars"]):
        schedule.calendars.append(
            Calendar(
                id=cal["id"],
                name=cal["name"],
                working_weekdays=set(cal["working_weekdays"]),
                exceptions=[
                    CalendarException(day, working=False)
                    for day in (as_date(h) for h in cal.get("holidays", []))
                    if day
                ],
                is_default=index == 0,
            )
        )
    schedule.default_calendar_id = schedule.calendars[0].id
    for entry in payload["activities"]:
        schedule.activities.append(
            ExchangeActivity(
                id=entry["id"],
                name=entry["name"],
                kind=ActivityKind(entry.get("kind", "task")),
                calendar_id=entry.get("calendar_id"),
                duration_days=int(entry.get("duration_days", 0)),
                constraint=parse_constraint(entry.get("constraint")),
                constraint_date=as_date(entry.get("constraint_date")),
                code=entry["id"],
            )
        )
        for pred in entry.get("predecessors", []):
            if isinstance(pred, str):
                schedule.relationships.append(ExchangeRelationship(pred, entry["id"]))
            else:
                schedule.relationships.append(
                    ExchangeRelationship(
                        pred["id"],
                        entry["id"],
                        RelationType(pred.get("type", "FS")),
                        int(pred.get("lag_days", 0)),
                    )
                )
    return schedule


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="massingplan", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("check", help="boot and report the resolved config").set_defaults(
        func=_cmd_check
    )

    schedule = sub.add_parser("schedule", help="schedule a .json, .xer or .xml file")
    schedule.add_argument("path")
    schedule.add_argument("--json", action="store_true", help="print the full result as JSON")
    schedule.set_defaults(func=_cmd_schedule)

    assess = sub.add_parser("assess", help="run the DCMA 14-point assessment on a .json network")
    assess.add_argument("path")
    assess.set_defaults(func=_cmd_assess)

    demo = sub.add_parser("demo", help="print the worked demo schedule")
    demo.add_argument("--progressed", action="store_true", help="the same job with actuals")
    demo.set_defaults(func=_cmd_demo)

    sub.add_parser(
        "init-db", help="migrate to head and seed the default organisation"
    ).set_defaults(func=_cmd_init_db)

    admin = sub.add_parser("create-admin", help="create an owner (password from the environment)")
    admin.add_argument("email")
    admin.add_argument("--name", default="")
    admin.set_defaults(func=_cmd_create_admin)

    sub.add_parser("seed-demo", help="create a demo account and project").set_defaults(
        func=_cmd_seed_demo
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
