"""Parsing XML from a stranger, with only the standard library.

`core` cannot have `defusedxml` -- it is copied verbatim into another codebase
and imports nothing outside the standard library -- so the defence is built
from what is there, and it has to be shown to work.
"""

from __future__ import annotations

import pytest

from massingplan.core.mspdi import MSPDIError, read_mspdi
from massingplan.core.p6xml import P6XMLError, read_p6xml
from massingplan.core.xmlsafe import looks_like_a_bomb, parse

BOMB = """<?xml version="1.0"?>
<!DOCTYPE lolz [
 <!ENTITY lol "lol">
 <!ENTITY lol1 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
 <!ENTITY lol2 "&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;&lol1;">
 <!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">
 <!ENTITY lol4 "&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;&lol3;">
]>
<APIBusinessObjects><Project><Name>&lol4;</Name></Project></APIBusinessObjects>"""


def test_the_expansion_this_guards_against_is_real() -> None:
    """Measured, not assumed, because the guard is only worth its weight if the
    thing it stops actually happens.

    `xml.etree` does not resolve *external* entities -- the attack most people
    mean by XXE -- but it does expand internal ones. Four levels turns 448
    bytes into 30,000 characters; each further level multiplies by ten.
    """
    from xml.etree import ElementTree

    root = ElementTree.fromstring(BOMB)  # noqa: S314 - demonstrating the vulnerability
    expanded = root.find("Project/Name")
    assert expanded is not None
    assert len(expanded.text or "") == 10_000 * 3
    assert len(BOMB) < 500, "a payload small enough that an upload limit does not see it"


def test_the_guard_refuses_it_unparsed() -> None:
    assert looks_like_a_bomb(BOMB)
    with pytest.raises(ValueError, match="declares XML entities"):
        parse(BOMB)


def test_both_readers_refuse_it_in_their_own_error_type() -> None:
    """The message names the format the user thought they were uploading."""
    with pytest.raises(P6XMLError, match="declares XML entities"):
        read_p6xml(BOMB)
    with pytest.raises(MSPDIError, match="declares XML entities"):
        read_mspdi(BOMB.replace("APIBusinessObjects", "Project"))


def test_an_ordinary_doctype_without_entities_is_left_alone() -> None:
    """Refusing every DOCTYPE would reject harmless documents to no purpose.

    ElementTree ignores external DTDs, so one that defines no entities cannot
    expand anything.
    """
    plain = (
        '<?xml version="1.0"?>\n<!DOCTYPE plan SYSTEM "plan.dtd">\n'
        "<APIBusinessObjects><Project><Id>X</Id></Project></APIBusinessObjects>"
    )
    assert not looks_like_a_bomb(plain)
    assert parse(plain).tag == "APIBusinessObjects"


def test_a_real_export_is_untouched() -> None:
    from tests.test_p6xml import activity, doc

    assert not looks_like_a_bomb(doc(activity("1", "A", 40.0)))


def test_the_scan_is_bounded_to_the_prologue() -> None:
    """A DTD is only legal before the root element, so there is no reason to
    scan a 60MB export to its end on every upload."""
    padded = "<APIBusinessObjects>" + ("<Pad/>" * 200_000) + "</APIBusinessObjects>"
    assert not looks_like_a_bomb(padded + "<!DOCTYPE x [<!ENTITY a 'b'>]>")
