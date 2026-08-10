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

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    result: int = args.func(args)
    return result


if __name__ == "__main__":
    raise SystemExit(main())
