"""Prove the adoption kit is drop-in, by assembling a consumer and running it.

The kit shipped twice with defects the consumer found, and both times the reason
was the same: **nothing here ever ran it in the consumer's shape.** The kit's own
`test_adoption.py` imports `aec_api`, so it cannot run in this repo, and
`test_mp_engine.py` is written for a host with no pytest. Between them they
covered everything except the question that matters — does this drop in?

So this builds a miniature of massing's layout in a temp directory:

    services/api/src/massingplan/core/...   the vendored engine
    services/api/src/aec_api/...            the adapter modules
    services/api/test_mp_engine.py          the conformance gate

and runs the gate the way that repo runs everything — `python test_mp_engine.py`,
a subprocess, **no pytest available to it**. If the kit needs something the
consumer does not have, this fails here instead of there.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
KIT = REPO / "massingplan" / "integrations" / "massing"
CORE = REPO / "massingplan" / "core"

#: Kit file -> where it lands under `services/api/`. Mirrors `ADAPTER_FILES` in
#: `scripts/vendor_to_massing.py`; a test asserts the two agree.
PLACEMENT = {
    "schedule_cpm.py": "src/aec_api/schedule_cpm.py",
    "schedule_engine.py": "src/aec_api/schedule_engine.py",
    "schedule_import.py": "src/aec_api/schedule_import.py",
    "test_mp_engine.py": "test_mp_engine.py",
}


@pytest.fixture
def consumer(tmp_path: Path) -> Path:
    """A miniature of `modelmaker/services/api`, assembled from the kit."""
    api = tmp_path / "services" / "api"
    (api / "src" / "aec_api").mkdir(parents=True)

    vendored = api / "src" / "massingplan"
    vendored.mkdir(parents=True)
    shutil.copytree(CORE, vendored / "core", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(REPO / "massingplan" / "__init__.py", vendored / "__init__.py")
    (vendored / "py.typed").write_text("", encoding="utf-8")

    (api / "src" / "aec_api" / "__init__.py").write_text("", encoding="utf-8")
    for name, relative in PLACEMENT.items():
        source = KIT / name
        assert source.is_file(), f"the kit is missing {name}"
        destination = api / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    return api


def _run(api: Path, script: str) -> subprocess.CompletedProcess[str]:
    """Run as massing's `run_tests.py` does: a subprocess, cwd at the api root.

    `PYTHONPATH` is cleared of this repo so the subprocess cannot accidentally
    import massingplan from the source tree instead of the vendored copy -- that
    would make the whole exercise prove nothing.
    """
    env = {**os.environ, "PYTHONPATH": "", "PYTHONDONTWRITEBYTECODE": "1"}
    return subprocess.run(
        [sys.executable, script],
        cwd=api,
        env=env,
        capture_output=True,
        text=True,
        timeout=300,
        check=False,
    )


def test_the_conformance_gate_passes_in_the_consumers_shape(consumer: Path) -> None:
    result = _run(consumer, "test_mp_engine.py")
    assert result.returncode == 0, (
        f"the kit's own gate failed in a clean consumer\n"
        f"--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert "checks passed" in result.stdout, result.stdout
    assert " FAIL " not in result.stdout and " ERROR " not in result.stdout, result.stdout


def test_the_gate_needs_no_pytest(consumer: Path) -> None:
    """massing deliberately has no pytest: `run_tests.py` shells out to
    `python test_x.py` and each file asserts in a `__main__` block. A vendored
    test that imports pytest cannot execute there at all -- which is how ten of
    them failed in under a second on first adoption.
    """
    source = (consumer / "test_mp_engine.py").read_text(encoding="utf-8")
    assert "import pytest" not in source
    assert "__main__" in source, "the gate needs a runner, not just test functions"

    # And prove it, rather than trusting the grep: run with pytest made
    # unimportable, exactly as it is over there.
    blocker = consumer / "sitecustomize.py"
    blocker.write_text(
        "import sys\n"
        "class _NoPytest:\n"
        "    def find_module(self, name, path=None):\n"
        "        if name == 'pytest' or name.startswith('pytest.'):\n"
        "            raise ImportError('pytest is not installed in this repo')\n"
        "        return None\n"
        "sys.meta_path.insert(0, _NoPytest())\n",
        encoding="utf-8",
    )
    result = _run(consumer, "test_mp_engine.py")
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_gate_actually_fails_when_the_engine_is_wrong(consumer: Path) -> None:
    """Guard the guard. A gate that cannot fail is a gate that proves nothing --
    and this one exists because a green gate ran over the exact bug it was
    supposed to catch.
    """
    engine = consumer / "src" / "aec_api" / "schedule_engine.py"
    source = engine.read_text(encoding="utf-8")
    # Reintroduce the shortest-ref reading: the bug that made a sequential chain
    # come back as a fully parallel network.
    broken = source.replace(
        "out: list[tuple[str, RelationType, int]] = [(token, RelationType.FS, 0)]",
        "out: list[tuple[str, RelationType, int]] = []",
    )
    assert broken != source, "the sabotage no longer matches the source"
    engine.write_text(broken, encoding="utf-8")

    result = _run(consumer, "test_mp_engine.py")
    assert result.returncode == 1, "the gate passed over a network with its logic dropped"
    assert "the chain was lost" in result.stdout, result.stdout


def test_no_kit_file_collides_with_a_name_massing_already_uses(consumer: Path) -> None:
    """`test_cpm`, `test_constraints` and `test_graph` exist flat in that repo.
    A bare stem in the manifest resolves to *their* file, so the vendored one
    never runs -- silently, with the suite green.
    """
    theirs = {"test_cpm", "test_constraints", "test_graph", "test_eot", "test_edge_cases"}
    ours = {Path(name).stem for name in PLACEMENT.values() if Path(name).name.startswith("test_")}
    assert not (ours & theirs), f"stem collision with the consumer: {sorted(ours & theirs)}"
    assert all(stem.startswith("test_mp_") for stem in ours), (
        f"vendored tests must be namespaced `test_mp_*`: {sorted(ours)}"
    )


def test_the_placement_map_matches_the_vendor_script() -> None:
    """Two copies of the same mapping is one copy too many, so this pins them
    together rather than letting the kit and the script drift.
    """
    script = (REPO / "scripts" / "vendor_to_massing.py").read_text(encoding="utf-8")
    for name, relative in PLACEMENT.items():
        assert f'"{name}"' in script, f"{name} is not in the vendor script's ADAPTER_FILES"
        assert f'"{relative}"' in script, f"{name} is placed differently by the vendor script"
