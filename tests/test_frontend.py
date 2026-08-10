"""The one JavaScript file, and the contract it reads.

Nothing here ever checked the frontend. No linter, no type checker, no CI job,
no test — and the consequence was not cosmetic: the Gantt drew **no dependency
arrows at all**, on every page, for as long as it had existed. `to_rows()` never
carried `predecessors`, the renderer read `(row.predecessors || [])`, and an
absent contract became an empty list. Nothing threw, nothing logged, and the
chart looked like a chart.

These tests pin the two halves together. `tsc --checkJs` (its own CI job) checks
the file against its own JSDoc; this checks that the JSDoc matches what the
server actually sends.
"""

from __future__ import annotations

import json
import re
from html import unescape
from pathlib import Path

import pytest

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings

GANTT = Path(__file__).resolve().parent.parent / "massingplan" / "static" / "js" / "gantt.js"
SOURCE = GANTT.read_text(encoding="utf-8")

#: The same file with comments removed. Several of these tests search for a
#: pattern that must not appear in the *code*, and the comments explaining why
#: it must not appear obviously contain it.
CODE = re.sub(r"//[^\n]*", "", re.sub(r"/\*.*?\*/", "", SOURCE, flags=re.S))


@pytest.fixture
def client(tmp_path):  # type: ignore[no-untyped-def]
    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'fe.db'}",
        )
    )
    application.config["TESTING"] = True
    database.create_all()
    return application.test_client()


def _chart_data(client) -> list[dict]:  # type: ignore[no-untyped-def]
    """The JSON the Gantt is actually handed, read off the rendered page."""
    html = client.get("/demo").get_data(as_text=True)
    match = re.search(r'data-activities="([^"]*)"', html)
    assert match, "the demo page has no gantt host"
    return json.loads(unescape(match.group(1)))


# -- the contract ----------------------------------------------------------


def test_the_renderer_declares_the_row_shape_it_reads() -> None:
    """A `@typedef` is the only place the JS states what it needs. Without it
    there is nothing for a type checker to check and nothing for this test to
    compare against.
    """
    assert "@typedef {object} Row" in SOURCE


def test_every_property_the_typedef_declares_is_actually_sent(client) -> None:  # type: ignore[no-untyped-def]
    """The half that was not written down is the half that went missing."""
    declared = set(re.findall(r"@property \{[^}]+\} (\w+)", SOURCE))
    assert declared, "the Row typedef declares no properties"

    sent = set(_chart_data(client)[0])
    missing = declared - sent
    assert not missing, (
        f"the renderer's Row typedef declares {sorted(missing)}, which "
        "`chart_rows()` does not send. That is the shape of the arrows bug: the "
        "renderer reads a key nobody supplies."
    )


def test_dependency_arrows_have_something_to_draw(client) -> None:  # type: ignore[no-untyped-def]
    """The regression. Zero arrows on a demo schedule that is nothing but a
    chain of dependencies is not an empty schedule, it is a broken renderer.
    """
    rows = _chart_data(client)
    arrows = sum(len(row["predecessors"]) for row in rows)
    assert arrows > 0, (
        "no activity carries a predecessor, so the Gantt draws no dependency "
        "arrows -- which is exactly what it did for weeks"
    )


def test_the_arrow_loop_does_not_swallow_a_missing_contract() -> None:
    """`(row.predecessors || [])` is what made the bug silent: absent became
    empty, and empty draws nothing. Reading the key directly means a server that
    stops sending it produces a visible error instead of a quiet omission.
    """
    assert "row.predecessors || []" not in CODE
    assert "predecessors.forEach" in CODE


def test_bars_are_labelled_with_the_planners_code(client) -> None:  # type: ignore[no-untyped-def]
    """On a stored project the internal id is 32 hex characters. The renderer
    used it as the bar label, so every bar read as a UUID.
    """
    assert "row.code || row.activity_id" in CODE
    assert "code" in _chart_data(client)[0]


# -- the CSP the whole design depends on -----------------------------------


def test_the_script_pulls_nothing_from_the_network() -> None:
    """No CDN, no font, no chart library. `default-src 'self'` is only worth
    having if the one script file honours it.
    """
    urls = re.findall(r"https?://[^\s\"')]+", SOURCE)
    assert urls == ["http://www.w3.org/2000/svg"], urls
    for forbidden in ("fetch(", "XMLHttpRequest", "import(", "eval(", "new Function"):
        assert forbidden not in CODE, forbidden


def test_the_geometry_map_is_prototype_safe() -> None:
    """Activity ids come from uploaded files. A row coded `constructor` would
    resolve against `Object.prototype` on a plain object literal and route an
    arrow to a garbage coordinate.
    """
    assert "Object.create(null)" in CODE


# -- the checker itself ----------------------------------------------------


def test_the_type_checker_is_configured_and_wired_into_ci() -> None:
    """`checkJs` off, or the job absent, and this file is unchecked again."""
    root = Path(__file__).resolve().parent.parent
    config = json.loads(
        re.sub(
            r'^\s*"//":\s*\[.*?\],\s*$', "", (root / "jsconfig.json").read_text(), flags=re.S | re.M
        )
    )
    options = config["compilerOptions"]
    assert options["checkJs"] is True
    assert options["strict"] is True
    assert options["noEmit"] is True, "a checker that emits would overwrite the served file"

    ci = (root / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert "jsconfig.json" in ci, "nothing type-checks the JavaScript in CI"
