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
* **A gate comes too.** A vendored library the consumer never exercises is a
  fork. What travels is `test_mp_engine.py` -- stdlib-only, flat, namespaced --
  and *not* upstream's pytest suite, which cannot run in a repo that has no
  pytest. See `ADAPTER_FILES` for why each of those three properties matters.
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
KIT = REPO / "massingplan" / "integrations" / "massing"
DEFAULT_TARGET = Path("C:/Server/modelmaker/services/api/src/massingplan")

#: The adapter modules, and where they land relative to `services/api/`. Kept
#: upstream because they change when *this* repo changes -- a new relationship
#: type, a renamed row key -- so they travel with the engine rather than being
#: maintained separately in the consumer.
ADAPTER_FILES = {
    "schedule_engine.py": "src/aec_api/schedule_engine.py",
    "schedule_cpm.py": "src/aec_api/schedule_cpm.py",
    "schedule_import.py": "src/aec_api/schedule_import.py",
    # Flat, and namespaced `test_mp_`. Both matter, and both were learned the
    # hard way on first adoption:
    #
    # *Flat*, because that repo's `run_tests.py` discovers with
    # `HERE.glob("test_*.py")` -- not recursive. A suite one directory down does
    # not run, and the gate reports green over whatever it was meant to catch.
    #
    # *Namespaced*, because `test_cpm`, `test_constraints` and `test_graph`
    # already exist flat over there. A bare stem resolves to their file, and the
    # vendored one silently never runs.
    "test_mp_engine.py": "test_mp_engine.py",
}

#: Upstream's own pytest modules are **not** copied, and there is no list of
#: them here to go stale.
#:
#: The consumer deliberately has no pytest -- `run_tests.py` shells out to
#: `python test_x.py` and each file asserts in a `__main__` block. Copying a
#: pytest suite there means every module dies on `import pytest` before it runs
#: an assertion, which is what happened: ten modules failed in under a second,
#: including the one covering the defect the adoption exists to fix.
#:
#: Adding pytest to that repo to make a vendored kit run would be the tail
#: wagging the dog. So the consumer gets `test_mp_engine.py`, a stdlib-only
#: conformance gate sized to what its callers actually depend on (see
#: ADAPTER_FILES), and the full suite stays here where it runs on every push.


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

**"Pure standard library" describes this subtree, not the upstream project.**
`massingplan/core/` imports nothing but the standard library, and an
import-linter contract plus a dependency-free CI job hold it that way, because
that is the property this copy depends on. The upstream *project* around it
declares Flask, SQLAlchemy, Alembic and argon2-cffi for its web application —
so anyone verifying the claim should read this directory, not that
`pyproject.toml`. Checking the project would give the wrong answer.

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

`services/api/test_mp_engine.py` — a **stdlib-only** conformance gate. No pytest,
a `__main__` runner, flat placement, and a `test_mp_` prefix.

```bash
python test_mp_engine.py       # from services/api
```

Each of those three properties was learned on first adoption. Flat, because
`run_tests.py` discovers with a non-recursive glob and a suite one directory
down does not run — silently, with the gate green over the very defect it was
meant to catch. Prefixed, because `test_cpm`, `test_constraints` and
`test_graph` already exist flat here, so a bare stem resolves to the local file.
Stdlib, because this repo deliberately has no pytest and a vendored suite that
imports it dies before its first assertion.

**Register it with `run_tests.py`.** It is not discovered by accident, and a
gate nobody runs is worse than no gate — it reads as coverage.

The gate is not upstream's suite. massingplan runs roughly 780 tests on every
push, including a 100%-branch-coverage job on the calendar kernel; duplicating
those here would create two copies to keep in step. What this file answers is
the narrower question: does the copy in `src/massingplan` still behave the way
*this* repo's callers require? It checks the calendar adjoint invariant, the
`compute()` dict contract, that a sequential chain sums, that float is numeric
for completed work, that a cycle refuses rather than inventing dates, and that
`TASKPRED` is read on import.
"""


def sync(target: Path, *, check: bool, engine_only: bool = False) -> int:
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

    # Remove the old nested pytest suite if a previous sync left one. It never
    # ran there -- `run_tests.py` globs one level and the modules died on
    # `import pytest` anyway -- and leaving it looks like coverage that exists.
    stale = target.parent.parent / "tests" / "vendor_massingplan"
    if stale.exists():
        shutil.rmtree(stale)

    # The adapter. `--engine-only` leaves it alone, for a re-sync that should
    # pick up engine fixes without touching adapter code the consumer may be
    # mid-review on.
    adapters = 0
    if not engine_only:
        api_root = target.parent.parent
        for name, relative in ADAPTER_FILES.items():
            source_file = KIT / name
            if not source_file.is_file():
                continue
            destination = api_root / relative
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source_file, destination)
            adapters += 1

    files = len(list(target.rglob("*.py")))
    print(f"vendored {files} modules to {target}")
    if adapters:
        print(f"copied {adapters} adapter modules into {target.parent.parent}")
        print(
            "still to apply by hand: integrations/massing/research-importer.patch "
            "(the P6 importer) and vendor-massingplan-drift.yml"
        )
        print(
            "register test_mp_engine.py with run_tests.py -- it is flat and "
            "stdlib-only, so `python test_mp_engine.py` is all it needs"
        )
    print(f"pinned at {sha} (digest {tree_digest(SOURCE)})")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--target", type=Path, default=DEFAULT_TARGET)
    parser.add_argument("--check", action="store_true", help="report drift, change nothing")
    parser.add_argument(
        "--engine-only",
        action="store_true",
        help="sync core/ but leave the adapter alone (for a mid-review consumer)",
    )
    args = parser.parse_args()
    return sync(args.target, check=args.check, engine_only=args.engine_only)


if __name__ == "__main__":
    raise SystemExit(main())
