#!/usr/bin/env python3
"""Copy `massingplan/core/` verbatim into ibuilder/massing's source tree.

This mirrors the Python side of the pattern that repo already uses twice, for
`massingifc` and `massingpdf` in `apps/web/src/vendor/`: copy the source
unchanged, pin the upstream commit in a `VENDOR.md`, run upstream's own tests in
the consumer, and let a scheduled job complain when the pin goes stale.

Why vendoring rather than a package dependency: massing's API image installs a
hash-pinned `requirements.lock`, so every upstream version bump is a lock
regeneration. `services/api/src` is already on `PYTHONPATH`, so a directory
copied there imports with no packaging change at all. Publishing
`massingplan-core` to PyPI is the follow-on, not the blocker -- and because
`core` is pure standard library, the switch is a one-line change either way.

Rules this script enforces, because a vendored tree that drifts is a fork
nobody has noticed yet:

* **Verbatim.** No rewriting of imports, no local patches. `core` uses relative
  imports throughout precisely so this copy needs no edits.
* **Tests come too.** A vendored library the consumer never exercises is a fork.
* **The pin is recorded.** `VENDOR.md` carries the upstream SHA and the exact
  commands to re-sync.

Usage::

    python scripts/vendor_to_massing.py                 # default target
    python scripts/vendor_to_massing.py --target PATH
    python scripts/vendor_to_massing.py --check         # report drift, change nothing
"""

from __future__ import annotations

import argparse
import filecmp
import hashlib
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SOURCE = REPO / "massingplan" / "core"
DEFAULT_TARGET = Path("C:/Server/modelmaker/services/api/src/massingplan")

#: The core test modules the consumer runs. Excluded: tests that exercise the
#: Flask app, which does not exist over there.
VENDORED_TESTS = [
    "conftest.py",
    "test_timeaxis_kernel.py",
    "test_units.py",
    "test_graph.py",
    "test_constraints.py",
    "test_cpm.py",
    "test_schedule.py",
    "test_health_progress_risk.py",
    "test_xer.py",
    "test_mspdi.py",
    "test_levelling.py",
    "test_compare.py",
]


def upstream_sha() -> str:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        )
        return out.stdout.strip()
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Honest rather than fabricated. A VENDOR.md claiming a SHA that does
        # not exist is worse than one that says the tree was uncommitted.
        return "UNCOMMITTED-WORKING-TREE"


def working_tree_dirty() -> bool:
    try:
        out = subprocess.run(
            ["git", "-C", str(REPO), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        )
        return bool(out.stdout.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        return True


def tree_digest(root: Path) -> str:
    """A content hash over the vendored files, so drift is detectable cheaply."""
    digest = hashlib.sha256()
    for path in sorted(root.rglob("*.py")):
        digest.update(path.relative_to(root).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()[:16]


VENDOR_MD = """# Vendored: massingplan core

`massingplan/core/` from **MassingCloud/massingplan**, copied verbatim.

| | |
|---|---|
| Upstream | https://github.com/MassingCloud/massingplan |
| Commit | `{sha}` |
| Synced | {synced} |
| Content digest | `{digest}` |
| Local deviations | **NONE** |

## What this is

A pure-standard-library construction scheduling engine: multi-calendar CPM with
all four relationship types and all ten constraint types, data date and
progressed logic, DCMA 14-point quality assessment, Monte Carlo risk, resource
levelling, baseline comparison with delay attribution, and Primavera XER /
MS Project MSPDI interchange.

It replaces `aec_api/schedule_cpm.py`, which was Finish-to-Start only with no
lags, no calendars and no constraints, and which never wrote computed dates
back to the activities.

## Why it is vendored rather than installed

`services/api/src` is already on `PYTHONPATH` (see `services/api/Dockerfile`),
and `core` imports nothing outside the standard library, so this directory works
here with no packaging change. The API image installs a hash-pinned
`requirements.lock`, which would make every upstream bump a lock regeneration.

Publishing `massingplan-core` to PyPI is roadmapped; because the package is pure
stdlib, the switch is a one-line change.

## Do not edit these files here

Fix it upstream and re-sync. A local patch makes this a fork, and the next sync
silently reverts it.

## Re-syncing

From a massingplan checkout:

```bash
python scripts/vendor_to_massing.py --target {target}
```

Or by hand:

```bash
rm -rf services/api/src/massingplan
cp -r <massingplan>/massingplan/core services/api/src/massingplan
cp <massingplan>/massingplan/py.typed services/api/src/massingplan/py.typed
```

## Tests

Upstream's own test modules are copied to `services/api/tests/vendor_massingplan/`
and run by this repo's pytest. That is the drift detector: a vendored library
nobody exercises is a fork you have not noticed yet.

```bash
pytest services/api/tests/vendor_massingplan -q
```
"""


def sync(target: Path, *, check: bool) -> int:
    if not SOURCE.is_dir():
        print(f"source not found: {SOURCE}", file=sys.stderr)
        return 2

    if check:
        if not (target / "core").is_dir():
            print(f"DRIFT: {target / 'core'} does not exist")
            return 1
        comparison = filecmp.dircmp(str(SOURCE), str(target / "core"))
        drifted = [
            *comparison.left_only,
            *[f for f in comparison.right_only if f not in ("VENDOR.md", "py.typed")],
            *comparison.diff_files,
        ]
        if drifted:
            print("DRIFT between upstream and the vendored copy:")
            for name in sorted(drifted):
                print(f"  {name}")
            return 1
        print(f"in sync ({tree_digest(SOURCE)})")
        return 0

    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        shutil.rmtree(target)
    # The package shape is preserved -- `massingplan/core/`, not a flattened
    # `massingplan/`. Upstream's modules import each other relatively, but its
    # *tests* import `massingplan.core.timeaxis` absolutely, and those tests are
    # copied over unchanged. Flattening here would force an edit to every test
    # file, which is exactly the local deviation this pattern exists to forbid.
    target.mkdir(parents=True)
    shutil.copytree(SOURCE, target / "core", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(REPO / "massingplan" / "__init__.py", target / "__init__.py")
    (target / "py.typed").write_text("", encoding="utf-8")

    sha = upstream_sha()
    if working_tree_dirty():
        print(
            "warning: the massingplan working tree has uncommitted changes, so the "
            "pinned SHA does not describe what was copied. Commit before syncing "
            "for a re-creatable pin.",
            file=sys.stderr,
        )
    (target / "VENDOR.md").write_text(
        VENDOR_MD.format(
            sha=sha,
            synced=datetime.now(tz=timezone.utc).date().isoformat(),
            digest=tree_digest(SOURCE),
            target=target.as_posix(),
        ),
        encoding="utf-8",
    )

    # Upstream's own tests, so the consumer exercises what it vendored.
    tests_target = target.parent.parent / "tests" / "vendor_massingplan"
    if tests_target.exists():
        shutil.rmtree(tests_target)
    tests_target.mkdir(parents=True, exist_ok=True)
    copied = 0
    for name in VENDORED_TESTS:
        source_test = REPO / "tests" / name
        if source_test.is_file():
            shutil.copy2(source_test, tests_target / name)
            copied += 1
    (tests_target / "README.md").write_text(
        "# Vendored tests\n\n"
        "Copied verbatim from MassingCloud/massingplan by "
        "`scripts/vendor_to_massing.py`. Do not edit here -- fix upstream and\n"
        "re-sync. They run in this repo so that a drifted vendor copy fails a\n"
        "build rather than being discovered in production.\n",
        encoding="utf-8",
    )

    files = len(list(target.rglob("*.py")))
    print(f"vendored {files} modules and {copied} test modules to {target}")
    print(f"pinned at {sha} (digest {tree_digest(SOURCE)})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--check", action="store_true", help="report drift, change nothing")
    args = parser.parse_args()
    return sync(args.target, check=args.check)


if __name__ == "__main__":
    raise SystemExit(main())
