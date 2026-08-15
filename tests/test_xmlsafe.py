"""Parsing XML from a stranger, with only the standard library.

`core` cannot have `defusedxml` -- it is copied verbatim into another codebase
and imports nothing outside the standard library -- so the defence is built
from what is there, and it has to be shown to work.
"""

from __future__ import annotations

import pytest

from massingplan.core.mspdi import MSPDIError, read_mspdi
from massingplan.core.p6xml import P6XMLError, read_p6xml
from massingplan.core.xmlsafe import looks_like_a_bomb, parse, text

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


# --- the two ways the guard used to be walked around ------------------------
#
# It was a regex over the raw bytes, and both holes are in the *grammar* rather
# than in the pattern's intent, which is why they read as correct. They are
# pinned separately because the fixes are unrelated: one is about lexing the
# internal subset, the other about where the scan stops.

NESTED = (
    '<!ENTITY a "' + "x" * 64 + '">'
    '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
    '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
)


def test_a_bracket_inside_a_comment_does_not_end_the_internal_subset() -> None:
    """The old pattern walked the subset with `[^\\]]*`, so the first `]` ended
    the match -- and `]` is ordinary text inside a comment.

    Nothing about `<!DOCTYPE r [<!-- ] -->` looks like an attack, which is the
    point: the payload after it was never examined.
    """
    smuggled = f"<!DOCTYPE r [<!-- ] -->{NESTED}]><r>&c;</r>"
    assert looks_like_a_bomb(smuggled)
    with pytest.raises(ValueError, match="declares XML entities"):
        parse(smuggled)


def test_a_declaration_beyond_the_first_64kb_is_still_found() -> None:
    """The old scan read a fixed 64KB window, so the attacker chose the other
    side of it with a comment as padding.

    The scan is still bounded -- see the test above this block -- but by the
    root element rather than by a byte count.
    """
    padded = "<!DOCTYPE r [<!--" + " " * 70_000 + f"-->{NESTED}]><r>&c;</r>"
    assert looks_like_a_bomb(padded)
    with pytest.raises(ValueError, match="declares XML entities"):
        parse(padded)


def test_a_bracket_inside_a_quoted_string_does_not_end_it_either() -> None:
    """Caught by the old pattern too, but only by luck: `<!ENTITY z` sits before
    the `]`, so the match completed for a reason unrelated to the quoting."""
    quoted = f'<!DOCTYPE r [<!ENTITY z "]">{NESTED}]><r>&c;</r>'
    assert looks_like_a_bomb(quoted)


def test_a_malformed_document_is_not_reported_as_a_bomb() -> None:
    """This function answers one question. A document that is simply broken gets
    the parse error that names what is broken, not a message about entities."""
    assert not looks_like_a_bomb("<r><unclosed></r>")
    assert not looks_like_a_bomb("not xml at all")


# --- the character set, which was wrong in both directions ------------------


def xml_1_0_permits(codepoint: int) -> bool:
    """The `Char` production, transcribed from the spec by hand.

    Written out rather than derived from `xmlsafe._ILLEGAL`, because a test that
    computes its expectation from the table under test asserts only that the
    table equals itself -- and would have passed on every version of it.
    """
    return (
        codepoint in (0x9, 0xA, 0xD)
        or 0x20 <= codepoint <= 0xD7FF
        or 0xE000 <= codepoint <= 0xFFFD
        or 0x10000 <= codepoint <= 0x10FFFF
    )


def test_removal_matches_the_spec_across_the_whole_basic_plane() -> None:
    """Exhaustive to U+FFFF, plus a sample above it. Cheap, and it is the only
    way to catch a boundary that is one out."""
    probes = [*range(0x11000), 0x10FFFF]
    wrong = [
        f"U+{codepoint:04X}"
        for codepoint in probes
        if (text("A" + chr(codepoint) + "B") == "A" + chr(codepoint) + "B")
        is not xml_1_0_permits(codepoint)
    ]
    assert not wrong, f"disagrees with XML 1.0 at {wrong[:12]}"


def test_the_characters_it_used_to_miss() -> None:
    """Both were total loss, and the surrogate was the worse of the two: it came
    out of the *encode* as a `UnicodeEncodeError`, so the export died with a
    traceback about a codec rather than anything about the schedule."""
    for codepoint in (0xD800, 0xDFFF, 0xFFFE, 0xFFFF):
        assert text(f"Level{chr(codepoint)}One") == "Level One"


def test_the_characters_it_used_to_remove_are_kept_and_round_trip() -> None:
    """XML 1.0 discourages these; discouraged is not forbidden. Replacing one
    silently is the defaulting this codebase refuses everywhere else -- so they
    are kept, and the keeping is proved by a real parse rather than argued."""
    from xml.etree import ElementTree

    for codepoint in (0x7F, 0x85, 0x9F, 0xFFFD):
        name = f"Level{chr(codepoint)}One"
        assert text(name) == name
        document = f'<?xml version="1.0" encoding="UTF-8"?><r><n>{text(name)}</n></r>'
        node = ElementTree.fromstring(document.encode("utf-8")).find("n")  # noqa: S314
        assert node is not None
        assert node.text == name


def test_illegal_characters_still_leave_a_word_break() -> None:
    """`Level 1<VT>Walls` stays two words. Escaping is not deleting."""
    assert text("Level 1\x0bWalls") == "Level 1 Walls"
