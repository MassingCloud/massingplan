"""The four sprint modules, reachable and refusing correctly.

An engine nobody can call is a library, and a refusal that arrives as a 500
reads as a bug in the tool rather than a fixable problem with the request.
"""

from __future__ import annotations

import json

import pytest

from massingplan.api.errors import ValidationFailed
from massingplan.api.schedules import apply_weather, compress, model_delay, schedule_portfolio

CHAIN = [
    {"id": "A", "duration_days": 10},
    {"id": "B", "duration_days": 10, "predecessors": ["A"]},
]


def test_the_modelled_method_has_no_default() -> None:
    """The two answer opposite questions. Guessing one hands somebody a
    counterfactual they did not ask for, with a method name saying they did."""
    body = {
        "data_date": "2026-06-01",
        "activities": CHAIN,
        "events": [{"id": "E1", "name": "Late possession", "duration_days": 5, "impacts": "A"}],
    }
    with pytest.raises(ValidationFailed, match="impacted_as_planned"):
        model_delay(body)

    result = model_delay({**body, "method": "impacted_as_planned"})
    assert result["mip"].startswith("AACE 29R-03 MIP 3.6")
    assert result["total_days"] == 5, "working days; `total_calendar_days` carries the elapsed 7"
    json.dumps(result)


def test_modelled_refusals_arrive_as_validation_errors() -> None:
    body = {"data_date": "2026-06-01", "activities": CHAIN, "method": "impacted_as_planned"}
    with pytest.raises(ValidationFailed, match="non-empty list"):
        model_delay({**body, "events": []})
    with pytest.raises(ValidationFailed, match="not in this network"):
        model_delay({**body, "events": [{"id": "E", "duration_days": 5, "impacts": "NOPE"}]})


def test_compression_returns_options_and_never_applies_them() -> None:
    result = compress(
        {
            "data_date": "2026-06-01",
            "activities": CHAIN,
            "target_days": 2,
            "costs": [{"activity_id": "A", "cost_per_day": 100.0, "max_days": 4}],
        }
    )
    assert result["meets_target"] is True
    assert result["total_cost"] == 200.0
    assert all(o["kind"] == "crash" for o in result["options"])
    json.dumps(result)

    with pytest.raises(ValidationFailed, match="target_days"):
        compress({"data_date": "2026-06-01", "activities": CHAIN})


def test_a_portfolio_keeps_each_project_separate() -> None:
    result = schedule_portfolio(
        {
            "data_date": "2026-06-01",
            "projects": [
                {"id": "ENABLING", "activities": [{"id": "A", "duration_days": 10}]},
                {"id": "MAIN", "activities": [{"id": "A", "duration_days": 20}]},
            ],
            "external_links": [
                {
                    "predecessor_project": "ENABLING",
                    "predecessor_id": "A",
                    "successor_project": "MAIN",
                    "successor_id": "A",
                }
            ],
        }
    )
    assert set(result["projects"]) == {"ENABLING", "MAIN"}
    assert result["external_link_count"] == 1
    assert all("::" not in r["activity_id"] for r in result["projects"]["MAIN"])
    json.dumps(result)

    with pytest.raises(ValidationFailed, match="not in this portfolio"):
        schedule_portfolio(
            {
                "projects": [{"id": "MAIN", "activities": [{"id": "A", "duration_days": 1}]}],
                "external_links": [
                    {
                        "predecessor_project": "GHOST",
                        "predecessor_id": "A",
                        "successor_project": "MAIN",
                        "successor_id": "A",
                    }
                ],
            }
        )


def test_weather_reports_both_runs_and_the_difference() -> None:
    """One run with the days already in it cannot produce the number that
    matters, which is what the allowance cost."""
    result = apply_weather(
        {
            "data_date": "2026-06-01",
            "start": "2026-06-01",
            "finish": "2026-08-31",
            "activities": [
                {"id": "A", "duration_days": 20},
                {"id": "B", "duration_days": 20, "predecessors": ["A"]},
            ],
            "allowances": [{"calendar_id": "STD", "days_by_month": {"6": 4, "7": 4}}],
        }
    )
    assert result["days_lost"] > 0
    assert (
        result["with_allowance"]["project_finish"] > result["without_allowance"]["project_finish"]
    )
    assert result["applied"][0]["total_days"] == 8
    json.dumps(result)


def test_a_weather_allowance_for_an_unknown_calendar_is_refused() -> None:
    with pytest.raises(ValidationFailed, match="not in this schedule"):
        apply_weather(
            {
                "data_date": "2026-06-01",
                "start": "2026-06-01",
                "finish": "2026-06-30",
                "activities": CHAIN,
                "allowances": [{"calendar_id": "NOPE", "days_by_month": {"6": 2}}],
            }
        )


def test_the_capability_listing_names_all_four() -> None:
    from massingplan.app import create_app
    from massingplan.blueprints.schedule_api import capabilities

    app = create_app()
    with app.test_request_context():
        features = capabilities().get_json()["features"]

    for named in (
        "modelled_delay_aace_29r03_mip_3_6_and_3_9",
        "weather_allowance",
        "schedule_compression",
        "multi_project_portfolio",
    ):
        assert named in features, f"{named} is missing from the capability listing"
