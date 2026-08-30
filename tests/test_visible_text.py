"""Visible-text extraction: what a reader sees, and which captured bytes said it.

Decision 0001 requires a `document-body` representation to be visible text
rather than source-native markup, and requires every segment to cite the
captured bytes it came from. These check both halves on documents small enough
to read: the layout the bounded segmenter is handed, and the run map that turns
a representation interval back into a rendition interval.
"""

from __future__ import annotations

import pytest

from docspec.processing.visible_text import (
    NO_VISIBLE_TEXT,
    UNPARSEABLE,
    HtmlVisibleTextExtractor,
    VisibleTextError,
    XmlVisibleTextExtractor,
    heading_level_and_title,
    is_atx_heading,
)

XML = XmlVisibleTextExtractor()
HTML = HtmlVisibleTextExtractor()

RULE = b"""<RULE>
    <PREAMB>
        <AGENCY TYPE="S">DEPARTMENT OF TRANSPORTATION</AGENCY>
        <SUBJECT>Airworthiness Directives</SUBJECT>
        <SUM>
            <HD SOURCE="HED">SUMMARY:</HD>
            <P>We propose <E T="03">certain</E> airplanes &amp; helicopters.</P>
        </SUM>
    </PREAMB>
</RULE>
"""

PAGE = (
    b"<html>\n<head>\n<title>Ignored</title>\n</head>\n"
    b"<body><pre>[Federal Register]\n\nFirst paragraph here.\n\n"
    b'Second paragraph with <a href="http://x">a link</a> inside.\n</pre>'
    b"<script>var hidden = 1;</script></body></html>\n"
)


def text(result) -> str:
    return result.content.decode("utf-8")


def test_xml_blocks_become_one_line_each_separated_by_a_blank_line() -> None:
    lines = text(XML.extract(RULE)).split("\n\n")
    assert lines == [
        "DEPARTMENT OF TRANSPORTATION",
        "# Airworthiness Directives",
        "## SUMMARY:",
        "We propose certain airplanes & helicopters.",
    ]


def test_an_element_owning_text_is_emitted_whole_with_its_inline_children() -> None:
    # `<P>` owns character data directly, so it is the block; `<E>` is inline
    # inside it and never becomes a block of its own.
    assert "We propose certain airplanes & helicopters." in text(XML.extract(RULE))
    assert "\n\ncertain\n\n" not in text(XML.extract(RULE))


def test_xml_heading_levels_come_from_the_declared_vocabulary() -> None:
    blocks = XML.extract(RULE).blocks
    headings = [block for block in blocks if block.kind == "heading"]
    assert [block.level for block in headings] == [1, 2]
    assert XML.extract(RULE).metadata["headingCount"] == 2
    assert XML.extract(RULE).metadata["rootTag"] == "RULE"


def test_every_heading_line_reads_as_a_heading_to_the_bounded_segmenter() -> None:
    result = XML.extract(RULE)
    body = result.content.decode("utf-8")
    for block in result.blocks:
        line = body.encode("utf-8")[block.representation_start : block.representation_end].decode("utf-8")
        assert is_atx_heading(line) is (block.kind == "heading")
    assert heading_level_and_title("## SUMMARY:") == (2, "SUMMARY:")


def test_a_representation_interval_maps_back_to_the_captured_bytes_that_said_it() -> None:
    result = XML.extract(RULE)
    start = result.content.index(b"We propose")
    end = start + len(b"We propose certain airplanes & helicopters.")
    rendition_start, rendition_end = result.rendition_range(start, end)
    slice_bytes = RULE[rendition_start:rendition_end]
    assert b"We propose" in slice_bytes
    assert b"helicopters" in slice_bytes
    # The span covers the entity reference that produced the ampersand rather
    # than the single byte the reader sees.
    assert b"&amp;" in slice_bytes


def test_no_run_claims_a_byte_the_source_never_supplied() -> None:
    result = XML.extract(RULE)
    for run in result.runs:
        assert 0 <= run.rendition_start < run.rendition_end <= len(RULE)
        assert run.representation_start < run.representation_end <= len(result.content)
    ordered = sorted(result.runs, key=lambda run: run.representation_start)
    for earlier, later in zip(ordered, ordered[1:], strict=False):
        assert earlier.representation_end <= later.representation_start


def test_html_keeps_its_paragraph_breaks_instead_of_reflowing_them() -> None:
    body = text(HTML.extract(PAGE))
    assert body.startswith("[Federal Register]\n\nFirst paragraph here.\n\n")
    assert "Second paragraph with a link inside." in body


def test_html_suppresses_the_head_and_the_script_a_reader_never_sees() -> None:
    body = text(HTML.extract(PAGE))
    assert "Ignored" not in body
    assert "var hidden" not in body


def test_html_runs_address_the_exact_source_bytes_they_copied() -> None:
    result = HTML.extract(PAGE)
    start = result.content.index(b"Second paragraph")
    end = start + len(b"Second paragraph with a link inside.")
    rendition_start, rendition_end = result.rendition_range(start, end)
    assert PAGE[rendition_start:rendition_end].startswith(b"Second paragraph with ")
    assert b"<a href=" in PAGE[rendition_start:rendition_end]


def test_a_non_ascii_source_maps_through_utf8_byte_offsets() -> None:
    # Every offset in this format counts UTF-8 BYTES, so the test asks in bytes
    # too; a codepoint index would be two short before it reached "second".
    page = "<html><body><pre>café naïve\n\nsecond — dash</pre></body></html>".encode()
    result = HTML.extract(page)
    start = result.content.index("second".encode())
    end = start + len("second — dash".encode())
    rendition_start, rendition_end = result.rendition_range(start, end)
    assert page[rendition_start:rendition_end].decode("utf-8") == "second — dash"


def test_unparseable_markup_is_refused_with_a_machine_legible_reason() -> None:
    with pytest.raises(VisibleTextError) as raised:
        XML.extract(b"<RULE><P>unclosed")
    assert raised.value.reason_code == UNPARSEABLE


@pytest.mark.parametrize(
    "source",
    [b"<RULE><EMPTY/></RULE>", b"<RULE>   \n  </RULE>"],
)
def test_markup_carrying_no_visible_text_is_refused(source: bytes) -> None:
    with pytest.raises(VisibleTextError) as raised:
        XML.extract(source)
    assert raised.value.reason_code == NO_VISIBLE_TEXT


def test_html_carrying_only_suppressed_content_is_refused() -> None:
    with pytest.raises(VisibleTextError) as raised:
        HTML.extract(b"<html><head><title>Only</title></head><body><script>x</script></body></html>")
    assert raised.value.reason_code == NO_VISIBLE_TEXT


def test_the_extractor_configuration_rides_inside_its_digest() -> None:
    other = XmlVisibleTextExtractor(heading_levels={"SUBJECT": 1})
    assert other.configuration_digest != XML.configuration_digest
    assert XmlVisibleTextExtractor().configuration_digest == XML.configuration_digest
