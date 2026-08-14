"""Several projects with links between them, worked by hand.

The rule this module exists to keep: **a project with no external links keeps
exactly the dates it has alone.** Every start and every finish.

Its *float* is a different matter and moves on purpose -- one merged network
has one deadline, the programme's, so a package finishing early carries the
slack it genuinely has against the completion it feeds. That distinction was
found by probing, not by design, and the two are separate tests here so
neither can be mistaken for the other.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.network import Link, RelationType, Task
from massingplan.core.portfolio import (
    ExternalLink,
    PortfolioError,
    Project,
    schedule_portfolio,
    shared_resource_demand,
)
from massingplan.core.resources import Demand
from massingplan.core.schedule import schedule_network

JUN1 = date(2026, 6, 1)


def enabling() -> Project:
    return Project(
        id="ENABLING",
        name="Enabling works",
        tasks=[Task("A1000", "Demolition", 10, "5D"), Task("A1010", "Piling", 15, "5D")],
        links=[Link("A1000", "A1010", RelationType.FS, 0)],
    )


def main_works() -> Project:
    return Project(
        id="MAIN",
        name="Main contract",
        # The same activity ids on purpose: two projects both having an A1000
        # is normal, and a merged network would silently give the second the
        # first's logic.
        tasks=[Task("A1000", "Substructure", 20, "5D"), Task("A1010", "Frame", 25, "5D")],
        links=[Link("A1000", "A1010", RelationType.FS, 0)],
    )


# -- the rule ---------------------------------------------------------------


def test_a_project_with_no_external_links_keeps_its_dates(five_day) -> None:  # type: ignore[no-untyped-def]
    """Every start and every finish, asserted row by row.

    A portfolio feature that moved a standalone *date* would be unusable. Float
    is a separate question, and the next two tests cover it -- because it does
    move, on purpose.
    """
    alone = schedule_network(
        list(main_works().tasks), list(main_works().links), {"5D": five_day}, data_date=JUN1
    )
    portfolio = schedule_portfolio([enabling(), main_works()], [], {"5D": five_day}, data_date=JUN1)

    by_id = {r["activity_id"]: r for r in alone.to_rows()}
    for row in portfolio.rows_for("MAIN"):
        was = by_id[row["activity_id"]]
        assert (row["start"], row["finish"]) == (was["start"], was["finish"])


def test_float_is_measured_against_the_programme_not_the_package(five_day) -> None:  # type: ignore[no-untyped-def]
    """And that is the answer, not a leak.

    ENABLING finishes well before the programme does, so inside the portfolio
    it carries real slack against the completion it feeds. Reporting its
    standalone zero float would be the fiction.

    Found by probing rather than reasoning: 56 of 150 random portfolios
    differed from their standalone runs, always on `late_start`,
    `late_finish`, `total_float_days`, `is_critical` and `is_longest_path`, and
    never on `start` or `finish`. The behaviour was right and this module's
    stated promise was too broad.
    """
    alone = schedule_network(
        list(enabling().tasks), list(enabling().links), {"5D": five_day}, data_date=JUN1
    )
    portfolio = schedule_portfolio([enabling(), main_works()], [], {"5D": five_day}, data_date=JUN1)

    standalone = {r["activity_id"]: r["total_float_days"] for r in alone.to_rows()}
    programme = {r["activity_id"]: r["total_float_days"] for r in portfolio.rows_for("ENABLING")}

    assert set(standalone) == set(programme)
    assert all(v == 0 for v in standalone.values()), "alone, ENABLING is all critical"
    assert all(v > 0 for v in programme.values()), "against the programme it has genuine slack"


def test_standalone_rows_give_the_package_its_own_float_back(five_day) -> None:  # type: ignore[no-untyped-def]
    """A package manager wants the slack inside their package; a programme
    manager wants the slack against the completion date. Different methods, so
    one cannot be mistaken for the other."""
    alone = schedule_network(
        list(enabling().tasks), list(enabling().links), {"5D": five_day}, data_date=JUN1
    )
    portfolio = schedule_portfolio([enabling(), main_works()], [], {"5D": five_day}, data_date=JUN1)

    assert (
        portfolio.standalone_rows_for("ENABLING", {"5D": five_day}, data_date=JUN1)
        == alone.to_rows()
    )


def test_the_same_activity_id_in_two_projects_stays_two_activities(five_day) -> None:  # type: ignore[no-untyped-def]
    """Both projects have an A1000 and an A1010 and they are different work."""
    result = schedule_portfolio([enabling(), main_works()], [], {"5D": five_day}, data_date=JUN1)

    assert set(result.dates["ENABLING"]) == {"A1000", "A1010"}
    assert set(result.dates["MAIN"]) == {"A1000", "A1010"}
    assert result.dates["ENABLING"]["A1010"].finish != result.dates["MAIN"]["A1010"].finish, (
        "the two A1010s took different durations and must not have merged"
    )


def test_activity_ids_come_back_unqualified(five_day) -> None:  # type: ignore[no-untyped-def]
    """A caller persisting these rows against their own tables must not get an
    id that exists nowhere in their data."""
    result = schedule_portfolio([enabling(), main_works()], [], {"5D": five_day}, data_date=JUN1)
    for row in result.rows_for("MAIN"):
        assert "::" not in str(row["activity_id"])
    assert {r["activity_id"] for r in result.rows_for("MAIN")} == {"A1000", "A1010"}


# -- what the links do ------------------------------------------------------


def test_an_external_link_pushes_the_downstream_project(five_day) -> None:  # type: ignore[no-untyped-def]
    """Piling has to finish before the substructure starts. Scheduled alone,
    both projects begin on the same Monday and the programme is fiction."""
    apart = schedule_portfolio([enabling(), main_works()], [], {"5D": five_day}, data_date=JUN1)
    linked = schedule_portfolio(
        [enabling(), main_works()],
        [ExternalLink("ENABLING", "A1010", "MAIN", "A1000", RelationType.FS, 0)],
        {"5D": five_day},
        data_date=JUN1,
    )

    assert apart.project_starts["MAIN"] == JUN1
    assert linked.project_starts["MAIN"] > apart.project_finishes["ENABLING"]
    assert linked.programme_finish > apart.programme_finish
    assert linked.project_finishes["ENABLING"] == apart.project_finishes["ENABLING"], (
        "the upstream project is not moved by having something depend on it"
    )


def test_the_driving_path_crosses_the_boundary(five_day) -> None:  # type: ignore[no-untyped-def]
    """The roadmap's acceptance criterion: a delay has to propagate across.

    Lengthening an activity in the *upstream* project must move the downstream
    project's finish, which only happens if one pass scheduled both.
    """
    slower = Project(
        id="ENABLING",
        tasks=[Task("A1000", "Demolition", 10, "5D"), Task("A1010", "Piling", 25, "5D")],
        links=[Link("A1000", "A1010", RelationType.FS, 0)],
    )
    link = [ExternalLink("ENABLING", "A1010", "MAIN", "A1000", RelationType.FS, 0)]

    before = schedule_portfolio([enabling(), main_works()], link, {"5D": five_day}, data_date=JUN1)
    after = schedule_portfolio([slower, main_works()], link, {"5D": five_day}, data_date=JUN1)

    assert after.project_finishes["MAIN"] > before.project_finishes["MAIN"]
    assert after.programme_finish > before.programme_finish


def test_the_crossing_activities_are_named(five_day) -> None:  # type: ignore[no-untyped-def]
    """ "Which delays crossed a boundary" is the question a portfolio is for."""
    result = schedule_portfolio(
        [enabling(), main_works()],
        [ExternalLink("ENABLING", "A1010", "MAIN", "A1000", RelationType.FS, 0)],
        {"5D": five_day},
        data_date=JUN1,
    )
    assert result.crossing_activities == ("ENABLING::A1010", "MAIN::A1000")
    assert result.summary()["external_link_count"] == 1


def test_the_project_setting_the_programme_date_is_flagged(five_day) -> None:  # type: ignore[no-untyped-def]
    """Compressing any other project moves nothing, and saying so is cheap."""
    result = schedule_portfolio([enabling(), main_works()], [], {"5D": five_day}, data_date=JUN1)
    assert result.issues.has("PORTFOLIO.DRIVES_PROGRAMME")
    driving = [i for i in result.issues.entries if i.code == "PORTFOLIO.DRIVES_PROGRAMME"]
    assert [i.row_key for i in driving] == ["MAIN"]


# -- what it refuses --------------------------------------------------------


def test_an_external_link_inside_one_project_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    """It would be reported as a boundary crossing when it crosses nothing."""
    with pytest.raises(PortfolioError, match="is inside"):
        schedule_portfolio(
            [enabling(), main_works()],
            [ExternalLink("MAIN", "A1000", "MAIN", "A1010", RelationType.FS, 0)],
            {"5D": five_day},
            data_date=JUN1,
        )


def test_a_link_to_a_project_that_is_not_here_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PortfolioError, match="not in this portfolio"):
        schedule_portfolio(
            [main_works()],
            [ExternalLink("ENABLING", "A1010", "MAIN", "A1000")],
            {"5D": five_day},
            data_date=JUN1,
        )


def test_a_link_to_an_activity_that_does_not_exist_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PortfolioError, match="no such activity"):
        schedule_portfolio(
            [enabling(), main_works()],
            [ExternalLink("ENABLING", "NOPE", "MAIN", "A1000")],
            {"5D": five_day},
            data_date=JUN1,
        )


def test_a_duplicate_project_id_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PortfolioError, match="appears twice"):
        schedule_portfolio([main_works(), main_works()], [], {"5D": five_day}, data_date=JUN1)


def test_a_project_id_containing_the_separator_is_refused() -> None:
    """A collision there would merge two activities without a word."""
    with pytest.raises(PortfolioError, match="separate a project"):
        Project(id="A::B", tasks=[Task("X", "X", 1, "5D")])


def test_an_empty_portfolio_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    with pytest.raises(PortfolioError, match="at least one project"):
        schedule_portfolio([], [], {"5D": five_day}, data_date=JUN1)


# -- the shared pool --------------------------------------------------------


def test_demands_are_rekeyed_for_levelling_across_the_portfolio(five_day) -> None:  # type: ignore[no-untyped-def]
    """Levelling one pool needs one set of demands over one network."""
    result = schedule_portfolio([enabling(), main_works()], [], {"5D": five_day}, data_date=JUN1)
    rekeyed = shared_resource_demand(
        result,
        {
            "ENABLING": [Demand("A1010", "CRANE", 1.0)],
            "MAIN": [Demand("A1010", "CRANE", 1.0)],
        },
    )
    ids = sorted(d.activity_id for entries in rekeyed.values() for d in entries)
    assert ids == ["ENABLING::A1010", "MAIN::A1010"]


def test_a_demand_for_an_activity_not_in_that_project_is_refused(five_day) -> None:  # type: ignore[no-untyped-def]
    result = schedule_portfolio([enabling(), main_works()], [], {"5D": five_day}, data_date=JUN1)
    with pytest.raises(PortfolioError, match="not in that project"):
        shared_resource_demand(result, {"MAIN": [Demand("NOPE", "CRANE", 1.0)]})


def test_the_portfolio_does_not_level_itself(five_day) -> None:  # type: ignore[no-untyped-def]
    """Levelling moves dates by design. Doing it because a second project was
    loaded would change a programme nobody touched."""
    alone = schedule_network(
        list(main_works().tasks), list(main_works().links), {"5D": five_day}, data_date=JUN1
    )
    with_second_project = schedule_portfolio(
        [enabling(), main_works()], [], {"5D": five_day}, data_date=JUN1
    )
    by_id = {r["activity_id"]: r for r in alone.to_rows()}
    for row in with_second_project.rows_for("MAIN"):
        was = by_id[row["activity_id"]]
        assert (row["start"], row["finish"]) == (was["start"], was["finish"])


def test_the_summary_is_json_safe(five_day) -> None:  # type: ignore[no-untyped-def]
    import json

    result = schedule_portfolio(
        [enabling(), main_works()],
        [ExternalLink("ENABLING", "A1010", "MAIN", "A1000")],
        {"5D": five_day},
        data_date=JUN1,
    )
    payload = json.loads(json.dumps(result.summary()))
    assert payload["project_count"] == 2
    assert set(payload["project_finishes"]) == {"ENABLING", "MAIN"}
