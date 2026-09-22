"""Markup visible-text extraction must keep frozen layout parity while named corrections fix the old source map.

CDATA delimiters are excluded, equal-length whitespace interpolation is refused, and bare namespaces,
doctypes and invalid UTF-8 now refuse. Reader identity drift refuses before parsing, and native extractors
keep byte passthrough with recorded owner limits.
"""

from dataclasses import asdict
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
from xml.etree.ElementTree import fromstring

import pytest

from docspec.errors import IntegrityError
from docspec.processing import reader_identity
from docspec.processing.extraction import ExtractionError, HtmlExtractor, XmlExtractor
from docspec.processing.visible_text import (
    UNPARSEABLE, HtmlVisibleTextExtractor, VisibleTextError, XmlVisibleTextExtractor,
)
from docspec.processing.visible_text_runtime import VisibleTextExtractor
from tests.support import visible_text_oracle as old
from tests.support.processing import _captured
from tests.test_visible_text import PAGE, RULE

FIXTURES = Path(__file__).parent / 'fixtures/markup'


def _assert_runs(source, result):
    """Assert every run's byte range is in bounds, ``exact`` matches the sliced bytes, and exact runs
    map back through ``rendition_range``."""
    for run in result.runs:
        assert 0 <= run.rendition_start < run.rendition_end <= len(source)
        output = result.content[run.representation_start:run.representation_end]
        original = source[run.rendition_start:run.rendition_end]
        assert run.exact == (output == original)
        if run.exact:
            assert result.rendition_range(run.representation_start, run.representation_end) == (
                run.rendition_start, run.rendition_end,
            )


@pytest.mark.parametrize(('kind', 'name', 'digest'), [
    ('xml', 'annual-title1-edition.xml', '51af27ca7bd05393d27445704a46b925aecf01cf3ff687b26fdac9c268e7c9b3'),
    ('html', 'subject-index-45.html', '0c330f9fe3e7e1c441f6b52cecc1d17984be76b073b2ae692b8c068af462d795'),
])
@pytest.mark.parametrize('mutation', ['original', 'crlf', 'comments'])
def test_retained_markup_preserves_text_layout_and_source_evidence(kind, name, digest, mutation):
    source = (FIXTURES / name).read_bytes()
    assert sha256(source).hexdigest() == digest
    if mutation == 'crlf':
        source = source.replace(b'\n', b'\r\n')
    elif mutation == 'comments':
        source = source.replace(b'</', b'<!-- source boundary --></')
    reader, frozen = (XmlVisibleTextExtractor, old.XmlVisibleTextExtractor) if kind == 'xml' else (
        HtmlVisibleTextExtractor, old.HtmlVisibleTextExtractor,
    )
    result, previous = reader().extract(source), frozen().extract(source)
    assert result.content == previous.content
    assert [asdict(block) for block in result.blocks] == [asdict(block) for block in previous.blocks]
    assert result.metadata == previous.metadata
    _assert_runs(source, result)


@pytest.mark.parametrize(('reader', 'frozen', 'source'), [
    (XmlVisibleTextExtractor, old.XmlVisibleTextExtractor, RULE),
    (HtmlVisibleTextExtractor, old.HtmlVisibleTextExtractor, PAGE),
    (XmlVisibleTextExtractor, old.XmlVisibleTextExtractor, b'<RULE><P>caf&#233; &amp; <E>text</E>.</P></RULE>'),
    (HtmlVisibleTextExtractor, old.HtmlVisibleTextExtractor, '<p>café &amp; text<br>continues</p>'.encode()),
    (XmlVisibleTextExtractor, old.XmlVisibleTextExtractor, b'<RULE><P>' + b'A' * 65520 + b'&amp;B</P></RULE>'),
])
def test_existing_rule_page_inline_and_chunk_cases_keep_layout(reader, frozen, source):
    result, previous = reader().extract(source), frozen().extract(source)
    assert result.content == previous.content
    assert [asdict(block) for block in result.blocks] == [asdict(block) for block in previous.blocks]
    assert result.metadata == previous.metadata
    assert result.extractor_id.endswith('/v2')
    assert result.configuration_digest != previous.configuration_digest
    _assert_runs(source, result)


@pytest.mark.parametrize('boundary', [b'<!-- ignored -->', b'<?target ignored?>'])
def test_comments_and_processing_instructions_never_become_text_evidence(boundary):
    source = b'<RULE><P>A' + boundary + b'B</P></RULE>'
    result = XmlVisibleTextExtractor().extract(source)
    previous = old.XmlVisibleTextExtractor().extract(source)
    assert result.content == previous.content == b'AB'
    assert [source[run.rendition_start:run.rendition_end] for run in result.runs] == [b'A', b'B']
    assert [(run.rendition_start, run.rendition_end) for run in result.runs] == [
        (run.rendition_start, run.rendition_end) for run in previous.runs
    ]
    _assert_runs(source, result)


def test_named_cdata_mapping_correction_excludes_both_delimiters():
    source = b'<RULE><P>A<![CDATA[B<C]]>D</P></RULE>'
    result = XmlVisibleTextExtractor().extract(source)
    previous = old.XmlVisibleTextExtractor().extract(source)
    assert result.content == previous.content == b'AB<CD'
    assert [source[run.rendition_start:run.rendition_end] for run in result.runs] == [b'A', b'B<C', b'D']
    assert [source[run.rendition_start:run.rendition_end] for run in previous.runs] == [
        b'A<![CDATA[', b'B<C]]>', b'D',
    ]
    _assert_runs(source, result)


def test_named_equal_length_whitespace_correction_refuses_false_interpolation():
    source = b'<RULE><P>A\tB</P></RULE>'
    result, previous = XmlVisibleTextExtractor().extract(source), old.XmlVisibleTextExtractor().extract(source)
    assert result.content == previous.content == b'A B'
    assert previous.runs[0].exact is True
    assert result.runs[0].exact is False
    assert source[slice(*previous.rendition_range(1, 2))] == b'\t'
    assert source[slice(*result.rendition_range(1, 2))] == b'A\tB'
    _assert_runs(source, result)


def test_valid_namespace_keeps_literal_visible_name_and_expanded_native_root():
    source = b'<r:RULE xmlns:r="urn:publisher"><HD SOURCE="HED">Summary</HD><P>A</P></r:RULE>'
    visible = XmlVisibleTextExtractor().extract(source)
    assert visible.content == old.XmlVisibleTextExtractor().extract(source).content == b'## Summary\n\nA'
    assert visible.metadata['rootTag'] == 'r:RULE'
    captured = _captured(source, 'application/xml')
    native = XmlExtractor().extract(captured, source)
    assert native.receipt.metadata == {'rootTag': '{urn:publisher}RULE', 'elementCount': 3}
    assert native.payload.content == source
    assert native.payload.representation.blob.digest == captured.blob.digest


@pytest.mark.parametrize('source', [
    b'<r:RULE><P>A</P></r:RULE>',
    b'<!DOCTYPE RULE [<!ENTITY word "A">]><RULE><P>&word;</P></RULE>',
    b'<!DOCTYPE RULE []><RULE><P>A</P></RULE>',
])
def test_named_namespace_and_doctype_refusals_replace_old_visible_acceptance(source):
    assert old.XmlVisibleTextExtractor().extract(source).content == b'A'
    with pytest.raises(VisibleTextError) as failure:
        XmlVisibleTextExtractor().extract(source)
    assert failure.value.reason_code == UNPARSEABLE


@pytest.mark.parametrize('source', [
    b'<!DOCTYPE RULE SYSTEM "https://example.invalid/unused.dtd"><RULE><P>A</P></RULE>',
    (Path(__file__).parents[1] / 'examples/bill_fixtures/introduced.xml').read_bytes(),
])
def test_inert_external_doctype_keeps_existing_bill_example_parity_without_loading_dtd(source):
    result = XmlVisibleTextExtractor().extract(source)
    previous = old.XmlVisibleTextExtractor().extract(source)
    assert result.content == previous.content
    assert [asdict(block) for block in result.blocks] == [asdict(block) for block in previous.blocks]
    native = XmlExtractor().extract(_captured(source, 'application/xml'), source)
    assert native.payload.content == source
    assert native.receipt.metadata['rootTag'] == fromstring(source).tag
    _assert_runs(source, result)


def test_reader_identity_pins_the_reading_addresses_of_the_installed_wheel():
    identity = reader_identity.installed_reader_identity(reader_identity.MARKUP_MODULES)

    assert identity is not None
    assert [name for name, _ in identity[1]] == [
        "spicy_docs.reading.markup",
        "spicy_docs.reading.xml",
    ]
    for _name, digest in identity[1]:
        assert len(digest) == 64


@pytest.mark.parametrize('bound', ['max_bytes', 'max_events', 'max_depth'])
def test_owner_limits_are_recordable_source_refusals(monkeypatch, bound):
    from spicy_docs.reading import markup

    source = b'<RULE><P>A</P></RULE>'
    original = markup.read_xml_events
    limits = {'max_bytes': len(source) - 1, 'max_events': 3, 'max_depth': 1}
    monkeypatch.setattr(markup, 'read_xml_events', lambda body, **kwargs: original(body, **kwargs, **{bound: limits[bound]}))
    assert old.XmlVisibleTextExtractor().extract(source).content == b'A'
    with pytest.raises(VisibleTextError) as failure:
        XmlVisibleTextExtractor().extract(source)
    assert failure.value.reason_code == UNPARSEABLE
    with pytest.raises(ExtractionError):
        XmlExtractor().extract(_captured(source, 'application/xml'), source)


def test_native_html_retains_head_text_count_while_visible_output_suppresses_it():
    source = b'<html><head><title>Metadata</title></head><body><p>A &amp; B</p><script>hidden</script></body></html>'
    captured = _captured(source, 'text/html')
    native = HtmlExtractor().extract(captured, source)
    assert native.payload.content == source
    assert native.payload.representation.blob.digest == captured.blob.digest
    assert native.receipt.metadata == {'elementCount': 6, 'visibleUnicodeCodepointCount': len('MetadataA & B')}
    assert HtmlVisibleTextExtractor().extract(source).content == b'A & B'


def test_native_html_unmatched_end_cost_does_not_grow_with_open_void_tags():
    from docspec.processing.extraction import _html_visible_count

    comparisons = 0

    class CountedName(str):
        """``str`` subclass that counts equality comparisons, so the test can bound an unmatched-end scan."""
        __hash__ = str.__hash__

        def __eq__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__eq__(other)

    count = 2000
    events = [SimpleNamespace(kind='start', name=CountedName('br')) for _ in range(count)]
    events.extend(SimpleNamespace(kind='end', name='unmatched') for _ in range(count))
    events.append(SimpleNamespace(kind='text', text='Still visible'))
    assert _html_visible_count(events) == len('Still visible')
    assert comparisons < 4 * count


def test_native_utf8_gate_stays_while_xml_declaration_governs_decoded_namespace():
    source = '<?xml version="1.0" encoding="iso-8859-1"?><RULE xmlns="urn:café"><P>A</P></RULE>'.encode()
    old_root = fromstring(source.decode()).tag
    result = XmlExtractor().extract(_captured(source, 'application/xml'), source)
    assert old_root == '{urn:café}RULE'
    assert result.receipt.metadata['rootTag'] == fromstring(source).tag == '{urn:cafÃ©}RULE'
    assert result.payload.content == source
    latin1 = source.decode().encode('iso-8859-1')
    with pytest.raises(IntegrityError, match='UTF-8'):
        XmlExtractor().extract(_captured(latin1, 'application/xml'), latin1)


def test_named_invalid_utf8_html_refusal_classification_preserves_runtime_input_gate():
    source = b'<p>Bad byte: \xff</p>'
    captured = _captured(source, 'text/html')
    with pytest.raises(IntegrityError) as previous:
        old.HtmlVisibleTextExtractor().extract(source)
    assert type(previous.value) is IntegrityError
    with pytest.raises(VisibleTextError) as visible:
        HtmlVisibleTextExtractor().extract(source)
    assert type(visible.value) is VisibleTextError
    assert visible.value.reason_code == UNPARSEABLE
    with pytest.raises(ExtractionError) as native:
        HtmlExtractor().extract(captured, source)
    assert type(native.value) is ExtractionError
    with pytest.raises(UnicodeDecodeError):
        VisibleTextExtractor().extract(captured, source)


@pytest.mark.parametrize('change', ['version', 'module-bytes', 'missing'])
@pytest.mark.parametrize('operation', ['selected', 'extract', 'resolver', 'configuration'])
def test_runtime_identity_drift_refuses_before_parsing(monkeypatch, change, operation):
    from spicy_docs.reading import markup

    source = b'<RULE><P>A</P></RULE>'
    captured = _captured(source, 'application/xml')
    extractor = VisibleTextExtractor()
    identity = reader_identity.installed_reader_identity(reader_identity.MARKUP_MODULES)
    assert identity is not None
    changed = {
        'version': ('different', identity[1]),
        'module-bytes': (identity[0], ((identity[1][0][0], 'a' * 64), *identity[1][1:])),
        'missing': None,
    }[change]
    monkeypatch.setattr(reader_identity, 'installed_reader_identity', lambda _: changed)
    monkeypatch.setattr(markup, 'read_xml_events', lambda *a, **k: pytest.fail('parsed after reader identity drift'))
    actions = {
        'selected': lambda: extractor.selected_identity(captured),
        'extract': lambda: extractor.extract(captured, source),
        'resolver': lambda: extractor.evidence_resolver(captured, source),
        'configuration': lambda: extractor.configuration_digest,
    }
    with pytest.raises(IntegrityError, match='reader'):
        actions[operation]()


def test_runtime_mapping_v2_retains_checkable_block_bytes():
    source = b'<RULE><P>A<![CDATA[B<C]]>D</P></RULE>'
    captured = _captured(source, 'application/xml')
    extractor = VisibleTextExtractor()
    result = extractor.extract(captured, source)
    assert result.receipt.extractor_id == 'docspec.xml-visible-text-blocks/v2'
    resolver = extractor.evidence_resolver(captured, source)
    for mapping in result.payload.representation.evidence_mappings:
        assert mapping.transformation == 'docspec-visible-text-block/v2'
        assert resolver(mapping, source) == result.payload.content[mapping.representation_start:mapping.representation_end]


def test_empty_html_preserves_zero_observations_and_visible_refusal():
    from docspec.processing.visible_text import NO_VISIBLE_TEXT

    source = b""
    captured = _captured(source, "text/html")
    native = HtmlExtractor().extract(captured, source)
    assert native.payload.content == source
    assert native.receipt.metadata == {"elementCount": 0, "visibleUnicodeCodepointCount": 0}
    for extractor in (old.HtmlVisibleTextExtractor(), HtmlVisibleTextExtractor()):
        with pytest.raises(IntegrityError) as failure:
            extractor.extract(source)
        assert failure.value.reason_code == NO_VISIBLE_TEXT
    with pytest.raises(ValueError, match=NO_VISIBLE_TEXT):
        VisibleTextExtractor().extract(captured, source)


def test_visible_html_unmatched_end_cost_is_linear_after_suppression(monkeypatch):
    from dataclasses import replace
    from spicy_docs.reading import markup
    from docspec.processing.visible_text import _parse_html

    comparisons = 0

    class CountedName(str):
        """``str`` subclass that counts equality comparisons, so the test can bound the unmatched-end
        scan after suppression."""
        __hash__ = str.__hash__

        def __eq__(self, other):
            nonlocal comparisons
            comparisons += 1
            return super().__eq__(other)

    count = 2000
    source = b"<head><x></head>" * count + b"</unmatched>" * count
    observed = markup.read_html_events(source)
    observed = replace(observed, events=tuple(
        replace(event, name=CountedName(event.name)) if event.name is not None else event
        for event in observed.events
    ))
    monkeypatch.setattr(markup, "read_html_events", lambda _: observed)
    root, element_count = _parse_html(source)
    assert root.parts == [] and element_count == count * 2
    assert comparisons < 20 * count
