"""Hostile text in every field, through every writer.

`write_xer` emitted newlines raw, so an activity named `Dig\\n%R\\t99\\t1\\tEVIL`
ended its own row and started another -- a well-formed file containing an
activity nobody added. That was found by a flaky test in the determinism job
rather than by looking, which is the argument for this file: the bug was one
instance of a class, and the class is "a text field a user controls reaches a
serialiser that has delimiters".

Three writers, three different delimiter sets, one property: **whatever is put
in a text field, the file must read back with the same number of activities and
the field's content must survive as data.** Structure intact, meaning
preserved.

The payloads are chosen per format, but every payload is thrown at every
writer, because the interesting failure is the one nobody predicted -- an XML
payload cannot forge an XER row and vice versa, and running the matrix costs
nothing.
"""

from __future__ import annotations

from datetime import date

import pytest

from massingplan.core.model import (
    Calendar,
    ExchangeActivity,
    ExchangeRelationship,
    ExchangeSchedule,
)
from massingplan.core.mspdi import read_mspdi, write_mspdi
from massingplan.core.network import ActivityKind, RelationType
from massingplan.core.p6xml import read_p6xml, write_p6xml
from massingplan.core.xer import read_xer, write_xer

JUN1 = date(2026, 6, 1)

#: One payload per format's structure, plus the classics. Each tries to close
#: whatever the format uses to delimit a record and open a new one.
PAYLOADS = {
    "xer_row": "Dig\n%R\t99\t1\tEVIL\tInjected\t8",
    "xer_table": "Dig\n%T\tTASK\n%F\ttask_id\n%R\t99",
    "xml_element": "Dig</Name></Task><Task><UID>99</UID><Name>EVIL",
    "xml_p6": "Dig</Name></Activity><Activity><ObjectId>99</ObjectId><Name>EVIL",
    "xml_entity": "Dig&lt;&amp;&gt;&quot;",
    "xml_cdata": "Dig]]><!--x-->",
    "tabs_only": "Dig\tTAB\tSEPARATED",
    "crlf": "Dig\r\nWindows",
    "quotes": "Dig \"double\" 'single' &ampersand",
    "control": "Dig\x0bvertical\x0cform",
}

WRITERS = {
    "xer": (write_xer, read_xer),
    "mspdi": (write_mspdi, read_mspdi),
    "p6xml": (write_p6xml, read_p6xml),
}


def schedule_with(payload: str) -> ExchangeSchedule:
    """Every user-controlled text field carrying the payload at once.

    All of them together rather than one at a time: a serialiser that escapes
    the activity name and forgets the calendar name is exactly as broken, and
    testing fields singly is how the forgotten one keeps passing.
    """
    return ExchangeSchedule(
        project_id="P1",
        project_name=payload,
        data_date=JUN1,
        planned_start=JUN1,
        default_calendar_id="C1",
        calendars=[Calendar(id="C1", name=payload, working_weekdays={0, 1, 2, 3, 4})],
        activities=[
            ExchangeActivity(
                id="A1",
                code=payload,
                name=payload,
                kind=ActivityKind.TASK,
                calendar_id="C1",
                duration_days=5,
                notes=payload,
            ),
            ExchangeActivity(
                id="A2",
                code="A2",
                name=payload,
                kind=ActivityKind.TASK,
                calendar_id="C1",
                duration_days=3,
            ),
        ],
        relationships=[ExchangeRelationship("A1", "A2", RelationType.FS, 0)],
    )


@pytest.mark.parametrize("writer", sorted(WRITERS))
@pytest.mark.parametrize("payload_name", sorted(PAYLOADS))
def test_no_payload_can_forge_a_record(writer: str, payload_name: str) -> None:
    """Two activities in, two activities out. Always.

    A forged record is the whole attack: the file stays well-formed, so nothing
    raises, and the schedule a planner opens has work in it that nobody added.
    Counting is the assertion because that is precisely what forging changes --
    a substring match cannot tell an injected row from a real one whose id
    happens to look similar, which is how the original test flaked.
    """
    write, read = WRITERS[writer]
    body = write(schedule_with(PAYLOADS[payload_name]))
    back = read(body)

    assert len(back.activities) == 2, (
        f"{writer} wrote {len(back.activities)} activities from 2: "
        f"the {payload_name} payload forged one"
    )
    assert {a.id for a in back.activities} == {"A1", "A2"}
    assert len(back.relationships) == 1


@pytest.mark.parametrize("writer", sorted(WRITERS))
@pytest.mark.parametrize("payload_name", sorted(PAYLOADS))
def test_the_payload_survives_as_data(writer: str, payload_name: str) -> None:
    """Escaping is not deleting.

    A writer that dropped every suspicious character would pass the test above
    and lose the planner's activity names. The words have to come back; only
    the characters that cannot survive the format may change.
    """
    payload = PAYLOADS[payload_name]
    write, read = WRITERS[writer]
    back = read(write(schedule_with(payload)))
    name = back.activities[0].name or ""

    # Whatever the format did to the delimiters, the words are still there and
    # in order. `Dig` is first in every payload and the last word is its tail.
    assert name.startswith("Dig"), f"{writer}/{payload_name} lost the start of the name: {name!r}"
    for word in ("EVIL", "Injected", "TAB", "Windows", "double", "vertical"):
        if word in payload:
            assert word in name, f"{writer}/{payload_name} dropped {word!r} from {name!r}"


@pytest.mark.parametrize("payload_name", sorted(PAYLOADS))
def test_every_xer_row_keeps_its_field_count(payload_name: str) -> None:
    """The structural property a forged row breaks, checked directly.

    XER declares each table's columns in a `%F` line. A row with a different
    number of tab-separated fields is either truncated or forged, and both are
    invisible to any assertion about content.
    """
    body = write_xer(schedule_with(PAYLOADS[payload_name]))

    columns: dict[str, int] = {}
    table = ""
    for line in body.splitlines():
        if line.startswith("%T\t"):
            table = line.split("\t", 1)[1]
        elif line.startswith("%F\t"):
            columns[table] = len(line.split("\t")) - 1
        elif line.startswith("%R\t"):
            width = len(line.split("\t")) - 1
            assert width == columns[table], (
                f"a {table} row has {width} fields against {columns[table]} columns"
            )


@pytest.mark.parametrize("payload_name", sorted(PAYLOADS))
def test_the_xml_writers_produce_parseable_documents(payload_name: str) -> None:
    """Not merely readable by our own reader -- well-formed to any parser.

    A writer could emit something our reader happens to tolerate and P6 or MS
    Project rejects, and the planner finds out when the file will not open.
    """
    from xml.etree import ElementTree

    for write in (write_mspdi, write_p6xml):
        body = write(schedule_with(PAYLOADS[payload_name]))
        ElementTree.fromstring(body)  # noqa: S314 - our own output, and the point is that it parses
