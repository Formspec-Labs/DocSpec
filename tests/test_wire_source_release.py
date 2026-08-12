"""The pinned wire schema set, its conformance bundles, and the gate they decide."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from docspec.adapters.source_catalog import LocalJsonlSourceCatalog, LocalSourceReleaseReader
from docspec.adapters.wire_source_release import (
    WIRE_FORMAT,
    WIRE_FORMAT_VERSION,
    WIRE_SCHEMA_ROLES,
    JsonSchemaWireSourceReleaseGate,
    WireReleaseBundlePin,
    WireReleasePins,
    load_wire_release_pins,
    read_wire_release_bundle,
)
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest
from docspec.errors import IntegrityError
from docspec.ports.source_release import SourceReleasePin

ROOT = Path(__file__).resolve().parents[1]
PINS_PATH = ROOT / "fixtures/wire/source-catalog-release-v1/pins.json"

CANDIDATE_ID = "urn:rulespec:core:2de89ad867a3794cc1006ef4cd0301248d48a719b5cbab1946f62c2c30ac0ec5"
VALID_RELEASE_ID = "urn:spicy-regs:source-catalog-release:v1:2bce80ff4f54251a54930ee86a1697d5e946f6f07f6b6b269ecefd0a8bafc8bc"

# The schema digests the candidate names, restated here so a re-pinned schema
# file fails in this repository rather than quietly changing a verdict.
SCHEMA_DIGESTS = {
    "schemas/source-catalog-release-v1.schema.json": "sha256:1b7f0ccdefe52973db97fb145e3893a43bf4b12dcf00630a13af87a3486f4bbf",
    "schemas/member-manifest-v1.schema.json": "sha256:c22acd4d8d397d2bd790ee42fc7a44f93fccce3b8ae39e5e378967035b075d4c",
    "schemas/source-items-v1.schema.json": "sha256:94c3953f0a615a94d3a6f6489d9095ab8882f82d677c1661a9b79d3642e90702",
}

# Each pinned invalid bundle, with the member and pointer this structural gate
# locates and the first diagnostic code the release owner's own validator
# records for the same bytes. Code parity is not asserted: agreeing on codes is
# the cross-product verdict-agreement step, not this gate's claim. What is
# asserted here is that every one of these bundles is refused.
INVALID_BUNDLES = [
    # Publisher's first diagnostic: invalid.format at release.json
    ("unknown-version", "release.json", "/formatVersion", "was expected"),
    # Publisher's first diagnostic: invalid.schema at data/source-items.json/2/selection
    ("missing-disposition", "data/source-items.json", "/2/selection", "is a required property"),
    # Publisher's first diagnostic: invalid.schema at data/source-items.json/2/selection/disposition
    ("unknown-disposition", "data/source-items.json", "/2/selection/disposition", "is not one of"),
]


@pytest.fixture(scope="module")
def pins() -> WireReleasePins:
    return load_wire_release_pins(PINS_PATH)


@pytest.fixture(scope="module")
def gate(pins: WireReleasePins) -> JsonSchemaWireSourceReleaseGate:
    return JsonSchemaWireSourceReleaseGate.from_pins(pins)


def _bundle(pins: WireReleasePins, name: str) -> WireReleaseBundlePin:
    return next(bundle for bundle in pins.bundles if bundle.name == name)


def _copy_pinned_tree(tmp_path: Path) -> Path:
    destination = tmp_path / "pinned"
    shutil.copytree(PINS_PATH.parent, destination, symlinks=False)
    return destination / PINS_PATH.name


def test_pinned_wire_schema_set_carries_the_candidate_digests(pins: WireReleasePins) -> None:
    assert pins.candidate_id == CANDIDATE_ID
    assert pins.wire_format == WIRE_FORMAT
    assert pins.wire_format_version == WIRE_FORMAT_VERSION
    assert set(pins.schemas) == set(WIRE_SCHEMA_ROLES)

    for relative, digest in SCHEMA_DIGESTS.items():
        assert sha256_digest((PINS_PATH.parent / relative).read_bytes()) == digest

    # The schema members each bundle carries are the same bytes as the pinned set,
    # so a bundle is checked against the schemas it declares.
    for bundle in pins.bundles:
        for relative, digest in SCHEMA_DIGESTS.items():
            assert sha256_digest((bundle.directory / relative).read_bytes()) == digest


def test_pinned_valid_wire_release_conforms_to_its_published_schemas(
    pins: WireReleasePins,
    gate: JsonSchemaWireSourceReleaseGate,
) -> None:
    valid = _bundle(pins, "valid")
    assert valid.conforms
    assert valid.release_id == VALID_RELEASE_ID

    bundle = read_wire_release_bundle(valid.directory)
    assert bundle.root["releaseId"] == VALID_RELEASE_ID
    assert bundle.root["format"] == WIRE_FORMAT
    assert bundle.manifest["counts"]["memberCount"] == len(bundle.manifest["members"])
    assert len(bundle.items) == bundle.root["content"]["counts"]["discoveredCount"] == 6

    conformance = gate.check(root=bundle.root, manifest=bundle.manifest, items=bundle.items)
    assert conformance.violations == ()
    assert conformance.conforms
    assert gate.check_bundle(valid.directory).conforms


@pytest.mark.parametrize(("name", "member", "pointer", "message"), INVALID_BUNDLES)
def test_each_pinned_invalid_wire_release_is_refused_with_a_located_violation(
    pins: WireReleasePins,
    gate: JsonSchemaWireSourceReleaseGate,
    name: str,
    member: str,
    pointer: str,
    message: str,
) -> None:
    bundle = _bundle(pins, name)
    assert not bundle.conforms

    conformance = gate.check_bundle(bundle.directory)

    assert not conformance.conforms
    assert conformance.violations
    first = conformance.violations[0]
    assert (first.member, first.pointer) == (member, pointer)
    assert message in first.message


def test_pinned_bundle_verdicts_are_exactly_what_the_gate_returns(
    pins: WireReleasePins,
    gate: JsonSchemaWireSourceReleaseGate,
) -> None:
    assert {bundle.name for bundle in pins.bundles} == {"valid", *(name for name, _, _, _ in INVALID_BUNDLES)}
    for bundle in pins.bundles:
        assert gate.check_bundle(bundle.directory).conforms is bundle.conforms
        if bundle.conforms:
            assert (bundle.upstream_code, bundle.upstream_path) == ("valid", None)
        else:
            assert bundle.upstream_code.startswith("invalid.")
            assert isinstance(bundle.upstream_path, str)


def test_pinned_wire_release_refuses_bytes_that_differ_from_their_digest(tmp_path: Path) -> None:
    pins_path = _copy_pinned_tree(tmp_path)
    assert load_wire_release_pins(pins_path).candidate_id == CANDIDATE_ID

    schema = pins_path.parent / "schemas/source-items-v1.schema.json"
    original = schema.read_bytes()
    schema.write_bytes(original.replace(b"sourceItemId", b"sourceItemID", 1))
    assert len(schema.read_bytes()) == len(original)
    with pytest.raises(IntegrityError, match="differs from its pinned digest"):
        load_wire_release_pins(pins_path)

    schema.write_bytes(original[:-1])
    with pytest.raises(IntegrityError, match="differs in size from its pin"):
        load_wire_release_pins(pins_path)

    schema.unlink()
    with pytest.raises(IntegrityError, match="must be a regular, non-symlink file"):
        load_wire_release_pins(pins_path)

    schema.write_bytes(original)
    assert load_wire_release_pins(pins_path).pins_id == load_wire_release_pins(PINS_PATH).pins_id


def test_pinned_wire_release_refuses_an_extra_file_or_an_altered_pins_file(tmp_path: Path) -> None:
    pins_path = _copy_pinned_tree(tmp_path)
    extra = pins_path.parent / "bundles/valid/undeclared.json"
    extra.write_bytes(b"{}\n")
    with pytest.raises(IntegrityError, match="missing or extra files"):
        load_wire_release_pins(pins_path)
    extra.unlink()

    document = json.loads(pins_path.read_bytes().decode("utf-8"))
    document["origin"]["candidateId"] = "urn:rulespec:core:" + "0" * 64
    pins_path.write_bytes(canonical_json_file_bytes(document))
    with pytest.raises(IntegrityError, match="identity differs from its canonical content"):
        load_wire_release_pins(pins_path)


def test_source_release_reader_screens_a_wire_format_release_on_the_injected_gate(
    tmp_path: Path,
    pins: WireReleasePins,
    gate: JsonSchemaWireSourceReleaseGate,
) -> None:
    catalogs = LocalJsonlSourceCatalog(tmp_path / "catalogs")
    gated = LocalSourceReleaseReader(catalogs, wire_gate=gate)
    ungated = LocalSourceReleaseReader(catalogs)

    # The local distribution is not the wire format, so an injected gate changes
    # nothing about how a local release is admitted.
    items = [SourceItem("a", "v1", (CandidateFile("main", "a.txt", "text/plain", expected_size=1),))]
    reference = catalogs.write(items)
    local = SourceReleasePin(reference.locator, reference.digest)
    assert gated.admit(local) == ungated.admit(local)
    assert list(gated.open(local).items) == items

    for bundle in pins.bundles:
        shutil.copytree(bundle.directory, catalogs.root / bundle.name)
        root = catalogs.root / bundle.name / "release.json"
        pin = SourceReleasePin(f"{bundle.name}/release.json", sha256_digest(root.read_bytes()))

        # Without the gate the reader refuses these bytes for the only reason it
        # can see: they are not one of its own distributions.
        with pytest.raises(IntegrityError, match="not canonical JSON"):
            ungated.admit(pin)

        if bundle.conforms:
            with pytest.raises(IntegrityError, match="a format this reader does not admit"):
                gated.admit(pin)
        else:
            with pytest.raises(IntegrityError, match="violates its published schema"):
                gated.admit(pin)
            with pytest.raises(IntegrityError, match="violates its published schema"):
                gated.open(pin)

        with pytest.raises(IntegrityError, match="differs from its pinned digest"):
            gated.admit(SourceReleasePin(pin.root, sha256_digest(b"other bytes")))
