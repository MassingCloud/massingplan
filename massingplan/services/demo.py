"""A worked mid-rise fragment, so the product demonstrates itself.

Deliberately exercises the things the two source engines could not represent --
two calendars, all four relationship types, a lag, a constraint, recorded
progress and a milestone -- so the demo page is a claim the engine has to keep
rather than a screenshot.

Seeded from code, not a database: the page works on a fresh clone with nothing
installed and nothing migrated.
"""

from __future__ import annotations

from typing import Any


def demo_payload() -> dict[str, Any]:
    return {
        "name": "Eight-storey mid-rise, substructure to topping out",
        "data_date": "2026-06-01",
        "calendars": [
            {
                "id": "5D",
                "name": "Mon-Fri",
                "working_weekdays": [0, 1, 2, 3, 4],
                # A Christmas shutdown, so the demo shows a calendar exception
                # doing something rather than merely existing.
                "holidays": [f"2026-12-{d:02d}" for d in range(21, 32)],
            },
            {"id": "6D", "name": "Mon-Sat (steel)", "working_weekdays": [0, 1, 2, 3, 4, 5]},
        ],
        "activities": [
            {
                "id": "A1000",
                "name": "Notice to proceed",
                "duration_days": 0,
                "kind": "start_milestone",
                "calendar_id": "5D",
            },
            {
                "id": "A1010",
                "name": "Site setup and hoarding",
                "duration_days": 10,
                "calendar_id": "5D",
                "predecessors": ["A1000"],
            },
            {
                "id": "A1020",
                "name": "Bulk excavation",
                "duration_days": 15,
                "calendar_id": "5D",
                "predecessors": [{"id": "A1010", "type": "SS", "lag_days": 3}],
            },
            {
                "id": "A1030",
                "name": "Piling",
                "duration_days": 20,
                "calendar_id": "6D",
                "predecessors": ["A1020"],
            },
            {
                "id": "A1040",
                "name": "Pile caps and ground beams",
                "duration_days": 18,
                "calendar_id": "5D",
                "predecessors": [{"id": "A1030", "type": "FS", "lag_days": 5}],
            },
            {
                "id": "A1050",
                "name": "Structural steel",
                "duration_days": 40,
                "calendar_id": "6D",
                "predecessors": ["A1040"],
            },
            {
                "id": "A1060",
                "name": "Metal deck and pours",
                "duration_days": 30,
                "calendar_id": "5D",
                "predecessors": [{"id": "A1050", "type": "SS", "lag_days": 10}],
            },
            {
                "id": "A1070",
                "name": "Curtain wall procurement",
                "duration_days": 60,
                "calendar_id": "5D",
                "predecessors": ["A1000"],
                "constraint": "start_on_or_after",
                "constraint_date": "2026-07-01",
            },
            {
                "id": "A1080",
                "name": "Curtain wall install",
                "duration_days": 35,
                "calendar_id": "5D",
                "predecessors": ["A1070", {"id": "A1060", "type": "FF", "lag_days": 0}],
            },
            {
                "id": "A1090",
                "name": "Roofing",
                "duration_days": 12,
                "calendar_id": "5D",
                "predecessors": ["A1060"],
            },
            {
                "id": "A2000",
                "name": "Topping out",
                "duration_days": 0,
                "kind": "finish_milestone",
                "calendar_id": "5D",
                "predecessors": ["A1080", "A1090"],
            },
        ],
        "options": {"progress_mode": "retained_logic", "lag_calendar": "predecessor"},
    }


def demo_progressed_payload() -> dict[str, Any]:
    """The same job three months on, with actuals -- including one out of sequence.

    The out-of-sequence activity is the point: it is what makes the
    retained-logic / progress-override switch produce two different, defensible
    finish dates from one file.
    """
    payload = demo_payload()
    payload["data_date"] = "2026-09-01"
    by_id = {a["id"]: a for a in payload["activities"]}
    by_id["A1000"]["actual_start"] = "2026-06-01"
    by_id["A1000"]["actual_finish"] = "2026-06-01"
    by_id["A1010"].update(actual_start="2026-06-01", actual_finish="2026-06-12")
    by_id["A1020"].update(actual_start="2026-06-08", actual_finish="2026-07-03")
    by_id["A1030"].update(actual_start="2026-07-06", remaining_days=6)
    # Started before its predecessor finished. Real, and the reason the mode matters.
    by_id["A1040"].update(actual_start="2026-08-24", remaining_days=14)
    return payload


def linear_demo_payload() -> dict[str, Any]:
    """A worked location-based schedule: eight floors, five trades.

    Chosen so the chart shows the thing the chart is for. FRAME is the slow
    trade at three days a floor; PAINT is the fast one at a day. Painting cannot
    simply follow framing up the building -- it would overtake it by level 4 --
    so its whole line shifts right, fixed by the *top* floor rather than the
    bottom. That is the case a Gantt chart cannot show, and it is the reason the
    line shift takes its maximum over every location.

    Seeded from code rather than the database because locations are not
    persisted yet: the engine and the API exist, the storage does not, and a
    demo that pretended otherwise would be the kind of claim this repo has
    already had to walk back twice.
    """
    return {
        "start": "2026-06-01",
        "locations": [{"id": f"Level {i}", "sequence": i} for i in range(1, 9)],
        "tasks": [
            {"id": "Frame", "name": "Structural framing", "duration_days": 3},
            {"id": "MEP", "name": "MEP rough-in", "duration_days": 4, "buffer_days": 1},
            {"id": "Drywall", "name": "Drywall", "duration_days": 3, "buffer_days": 1},
            {"id": "Paint", "name": "Paint", "duration_days": 1, "buffer_days": 2},
            {"id": "Fitout", "name": "Fit-out", "duration_days": 2, "buffer_days": 1},
        ],
    }
