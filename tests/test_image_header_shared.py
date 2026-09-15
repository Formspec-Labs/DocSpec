"""Header observations preserve image bytes and name malformed-header corrections."""

from hashlib import sha256
from pathlib import Path

import pytest

from docspec.errors import IntegrityError
from docspec.processing import reader_identity
from docspec.processing.extraction import ImageExtractor
from tests.support.image_header_oracle import _image_dimensions as old_dimensions
from tests.support.processing import _captured

FIXTURES = Path(__file__).parent / 'fixtures/image-header'
PNG = b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x02\x80\x00\x00\x01\xe0'
SOF = b'\xff\xc0\x00\x0b\x08\x01\xe0\x02\x80\x01\x01\x11\x00'


def _extract(source, media_type='image/unknown'):
    captured = _captured(source, media_type)
    result = ImageExtractor().extract(captured, source)
    assert result.payload.content == source
    assert result.payload.representation.blob.digest == captured.blob.digest
    assert result.receipt.extractor_id == 'docspec.image-passthrough/v2'
    return result


@pytest.mark.parametrize(('format', 'digest'), [
    ('png', 'db00445fada92ec7fe72f084bb1cfe93cc685eaca32757fe58785f717e6f1854'),
    ('gif', '61974fde323e3e3ef4f5b38fd66d5d830c07c7bd68356aa67c8da7893a0f3f79'),
    ('jpeg', '349fe743d789c94e5da9edaef7d9f0975e656d77e7e1f08b7e31ec532400acfd'),
])
def test_complete_encoded_images_keep_old_dimensions_and_exact_evidence(format, digest):
    source = (FIXTURES / f'generated-31x17.{format}').read_bytes()
    assert sha256(source).hexdigest() == digest
    assert old_dimensions(source) == (format, 31, 17)
    result = _extract(source, 'image/' + format)
    assert result.receipt.metadata == {'imageFormat': format, 'widthPixels': 31, 'heightPixels': 17}
    evidence = result.payload.representation.evidence_mappings[0].evidence
    assert evidence.region == {'kind': 'whole-image', 'x': 0, 'y': 0, 'width': 31, 'height': 17, 'unit': 'pixel'}
    assert (evidence.start, evidence.end) == (0, len(source))


@pytest.mark.parametrize('source', [
    PNG, b'GIF87a\x80\x02\xe0\x01', b'GIF89a\x00\x00\x00\x00', b'\xff\xd8' + SOF,
    b'\xff\xd8\xff\xe0\x00\x04xx' + SOF, b'RIFFxxxxWEBP', b'not an image', PNG[:12],
])
def test_header_only_observations_match_old_reader_including_zero_and_unknown(source):
    format, width, height = old_dimensions(source)
    result = _extract(source)
    expected = {'imageFormat': format}
    if width is not None and height is not None:
        expected.update(widthPixels=width, heightPixels=height)
    assert result.receipt.metadata == expected


@pytest.mark.parametrize('source', [PNG[:12] + b'IDAT' + PNG[16:], PNG[:8] + b'\0\0\0\x0c' + PNG[12:]])
def test_named_png_header_correction_does_not_invent_dimensions_from_non_ihdr(source):
    assert old_dimensions(source) == ('png', 640, 480)
    result = _extract(source, 'image/png')
    assert result.receipt.metadata == {'imageFormat': 'png'}
    assert result.payload.representation.evidence_mappings[0].evidence.region == {'kind': 'whole-image'}


@pytest.mark.parametrize('prefix', [b'\xff\xd9', b'\xff\xda\x00\x02', b'junk', b'\xff\xd8'])
def test_named_jpeg_boundary_correction_does_not_borrow_later_dimensions(prefix):
    source = b'\xff\xd8' + prefix + SOF
    assert old_dimensions(source) == ('jpeg', 640, 480)
    assert _extract(source, 'image/jpeg').receipt.metadata == {'imageFormat': 'jpeg'}


@pytest.mark.parametrize('prefix', [b'\xff\xff\xff', b'\xff\x01'])
def test_named_valid_jpeg_fill_and_tem_markers_now_reach_the_frame(prefix):
    source = b'\xff\xd8' + prefix + SOF
    assert old_dimensions(source) == ('jpeg', None, None)
    assert _extract(source, 'image/jpeg').receipt.metadata == {
        'imageFormat': 'jpeg', 'widthPixels': 640, 'heightPixels': 480,
    }


@pytest.mark.parametrize('length', range(2, 7))
def test_named_short_first_jpeg_frame_cannot_borrow_a_later_frame(length):
    source = b'\xff\xd8\xff\xc0' + length.to_bytes(2, 'big') + bytes(length - 2) + SOF
    assert old_dimensions(source) == ('jpeg', 640, 480)
    assert _extract(source, 'image/jpeg').receipt.metadata == {'imageFormat': 'jpeg'}


@pytest.mark.parametrize('change', ['version', 'module-bytes', 'missing'])
def test_image_reader_drift_refuses_before_observation(monkeypatch, change):
    from spicy_docs.sources import image_header

    reader = ImageExtractor()
    identity = reader_identity.installed_reader_identity(reader_identity.IMAGE_MODULES)
    assert identity is not None
    changed = {
        'version': ('different', identity[1]),
        'module-bytes': (identity[0], ((identity[1][0][0], 'a' * 64),)),
        'missing': None,
    }[change]
    monkeypatch.setattr(reader_identity, 'installed_reader_identity', lambda _: changed)
    monkeypatch.setattr(image_header, 'read_image_header', lambda _: pytest.fail('read header after identity drift'))
    with pytest.raises(IntegrityError, match='reader'):
        reader.extract(_captured(PNG, 'image/png'), PNG)
