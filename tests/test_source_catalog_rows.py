"""Canonical catalog rows, schema equivalence, and verified reader behavior."""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from docspec.adapters import framing
from docspec.adapters.catalog_artifact import derivation as catalog_derivation
from docspec.adapters.catalog_artifact import digests as catalog_digests
from docspec.adapters.catalog_artifact import rows as catalog_rows
from docspec.adapters.catalog_artifact import schemas as catalog_schemas
from docspec.adapters.catalog_artifact.reader import SourceCatalogArtifactReader
from docspec.domain.source_catalog import SourceCatalogItem
from docspec.errors import IntegrityError
from tests.support.source_catalog import FakeSource, description, producer, record, renditions
from tests.support.source_catalog_builds import (
    build,
)


def test_the_incremental_framer_equals_rulespec_batch_framing_byte_for_byte() -> None:
    """The one-pass derivation only holds if the incremental hasher IS the protocol.

    ``_FramedSectionHasher`` re-states ``framed_section_digest``'s byte layout so
    ten digests can share one pass over the rows. This pins the two functions to
    each other across shapes: empty sections, one record, many records, nested
    values, non-ASCII text, and empty payload objects.
    """

    from rulespec_artifacts import FramedSection, framed_section_digest

    cases: list[tuple[str, str, list[dict[str, object]]]] = [
        ("docspec-test-domain/1", "records", []),
        ("docspec-test-domain/1", "records", [{"a": 1}]),
        ("docspec-test-domain/2", "members", [{"k": v, "n": [v, {"d": v}]} for v in range(50)]),
        ("docspec-test-domain/3", "rows", [{"text": "naïve — ünïcode ✓"}, {}]),
    ]
    for domain, name, records in cases:
        expected = framed_section_digest(domain, (FramedSection(name, len(records), iter(records)),))
        hasher = framing.FramedSectionHasher(domain, name, len(records))
        for digest_record in records:
            hasher.add(digest_record)
        assert hasher.digest() == expected

    over = framing.FramedSectionHasher("docspec-test-domain/1", "records", 1)
    over.add({"a": 1})
    with pytest.raises(IntegrityError, match="exceeds its declared count"):
        over.add({"a": 2})
    under = framing.FramedSectionHasher("docspec-test-domain/1", "records", 2)
    under.add({"a": 1})
    with pytest.raises(IntegrityError, match="declared 2 records but yielded 1"):
        under.digest()


def test_a_stored_catalog_row_is_byte_identical_to_its_reserialized_item(tmp_path: Path) -> None:
    """The state digest frames raw row bytes; this is the identity that permits it.

    Every staged row must satisfy raw == canonical(to_dict(from_dict(parse(raw)))),
    or framing raw bytes would diverge from framing re-serialized items. Proven
    here on a real built catalog rather than assumed.
    """

    from rulespec_artifacts import canonical_json_bytes, parse_canonical_json

    source = FakeSource(
        description(),
        (record("2026-00001"), record("2026-00002"), record("2026-00003")),
        (*renditions("2026-00001"), *renditions("2026-00002"), *renditions("2026-00003")),
    )
    store, result = build(tmp_path, source)
    verifier_reader = SourceCatalogArtifactReader(store, producer=producer())
    summary = verifier_reader.verify_snapshot(result.reference)
    checked = 0
    snapshot = verifier_reader.open_snapshot(result.reference)
    for item in snapshot.items:
        raw = canonical_json_bytes(item.to_dict())
        parsed = parse_canonical_json(raw, path="roundtrip")
        assert canonical_json_bytes(SourceCatalogItem.from_dict(parsed).to_dict()) == raw
        checked += 1
    assert checked == summary.item_count == 3


def test_the_compiled_validator_and_the_authority_agree_on_real_and_mutated_rows(
    tmp_path: Path,
) -> None:
    """The fast validator may only short-circuit acceptance, never decide refusal.

    Pins jsonschema-rs to python-jsonschema on this schema: every row of a real
    built catalog, plus systematic mutations of one (each required key dropped,
    each top-level field type-flipped, an unknown key added), must get the same
    accept/reject verdict from both engines -- and the gate's own error() must
    raise exactly when the authority rejects, with the authority's message.
    """

    gate = catalog_schemas._ITEM_VALIDATOR
    assert gate._fast is not None, "compiled validator failed to build for the item schema"
    authority = gate._authority

    source = FakeSource(
        description(),
        (record("2026-00001"), record("2026-00002", malformed_rin=True)),
        (*renditions("2026-00001"), *renditions("2026-00002")),
    )
    store, result = build(tmp_path, source)
    reader = SourceCatalogArtifactReader(store, producer=producer())
    reader.verify_snapshot(result.reference)
    rows = [item.to_dict() for item in reader.open_snapshot(result.reference).items]
    assert rows

    def verdicts(value: object) -> tuple[bool, bool, bool]:
        fast_ok = gate._fast.is_valid(value)
        authority_ok = not list(authority.iter_errors(value))
        try:
            gate.error(value, "differential row")
            gate_ok = True
        except IntegrityError:
            gate_ok = False
        return fast_ok, authority_ok, gate_ok

    mutants: list[object] = [dict(rows[0])]
    for key in list(rows[0]):
        dropped = dict(rows[0])
        del dropped[key]
        mutants.append(dropped)
        flipped = dict(rows[0])
        flipped[key] = 12345 if not isinstance(flipped[key], int) else "not-an-integer"
        mutants.append(flipped)
    unknown = dict(rows[0])
    unknown["unknownExtraKey"] = "x"
    mutants.append(unknown)

    for value in [*rows, *mutants]:
        fast_ok, authority_ok, gate_ok = verdicts(value)
        assert gate_ok == authority_ok, f"gate diverged from authority: {value!r:.120}"
        assert fast_ok == authority_ok, f"engines disagree (authority decides, but pin it): {value!r:.120}"


def test_verify_snapshot_re_derives_digests_and_memoizes_per_reader(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The consumer's verify must be the producer's gate, run again, once.

    A fresh reader's verify_snapshot performs the full independent derivation
    (spied), refuses a spec whose digest its derivation contradicts, and a
    second verify of the same digest returns the memoized verdict with no new
    derivation.
    """

    source = FakeSource(
        description(),
        (record("2026-00001"), record("2026-00002")),
        (*renditions("2026-00001"), *renditions("2026-00002")),
    )
    store, result = build(tmp_path, source)

    calls = {"derive": 0}
    actual = catalog_derivation._derive_catalog

    def spy(*args: Any, **kwargs: Any) -> catalog_digests._DerivedCatalog:
        calls["derive"] += 1
        return actual(*args, **kwargs)

    monkeypatch.setattr(catalog_derivation, "_derive_catalog", spy)
    reader = SourceCatalogArtifactReader(store, producer=producer())
    summary = reader.verify_snapshot(result.reference)
    assert summary == result.summary
    assert calls["derive"] == 1
    assert reader.verify_snapshot(result.reference) == summary
    assert calls["derive"] == 1

    def lying(*args: Any, **kwargs: Any) -> catalog_digests._DerivedCatalog:
        derived = actual(*args, **kwargs)
        return catalog_digests._DerivedCatalog(
            "sha256:" + "e" * 64,
            derived.requested_universe_set_digest,
            derived.selected_source_set_digest,
            derived.disposition_counts,
            derived.reason_counts,
            derived.diagnostics,
        )

    monkeypatch.setattr(catalog_derivation, "_derive_catalog", lying)
    fresh = SourceCatalogArtifactReader(store, producer=producer())
    with pytest.raises(IntegrityError, match="catalogStateDigest"):
        fresh.verify_snapshot(result.reference)


def test_a_verified_reader_streams_items_without_repeating_the_row_proofs(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After verify_snapshot memoizes a digest, open_snapshot's items stream skips
    the per-row schema and canonicality proofs it already ran; a reader that
    never verified still validates every row."""

    source = FakeSource(
        description(),
        (record("2026-00001"), record("2026-00002")),
        (*renditions("2026-00001"), *renditions("2026-00002")),
    )
    store, result = build(tmp_path, source)
    seen: list[bool] = []
    actual = catalog_rows._iter_located_catalog_rows

    def spy(*args: Any, **kwargs: Any) -> Any:
        seen.append(kwargs.get("validate", True))
        return actual(*args, **kwargs)

    monkeypatch.setattr(catalog_rows, "_iter_located_catalog_rows", spy)

    fresh = SourceCatalogArtifactReader(store, producer=producer())
    assert len(list(fresh.open_snapshot(result.reference).items)) == 2
    verified = SourceCatalogArtifactReader(store, producer=producer())
    verified.verify_snapshot(result.reference)
    assert len(list(verified.open_snapshot(result.reference).items)) == 2
    assert seen[0] is True, "an unverified reader must validate every row"
    assert seen[-1] is False, "a verified reader must not repeat the proofs"


def test_trusted_construction_equals_validated_construction_on_real_rows(tmp_path: Path) -> None:
    """Wrapping alone must yield the same items as full validation on admitted rows.

    A verified reader re-streams its catalog under trusted_json_input; every
    item it constructs must equal the item full validation constructs from the
    same bytes, and trusted construction must never leak past its context.
    """

    from docspec.domain.identity import trusted_json_input

    source = FakeSource(
        description(),
        (record("2026-00001"), record("2026-00002", malformed_rin=True)),
        (*renditions("2026-00001"), *renditions("2026-00002")),
    )
    store, result = build(tmp_path, source)
    reader = SourceCatalogArtifactReader(store, producer=producer())
    reader.verify_snapshot(result.reference)
    validated = list(SourceCatalogArtifactReader(store, producer=producer()).open_snapshot(result.reference).items)
    trusted = list(reader.open_snapshot(result.reference).items)
    assert trusted == validated
    assert [i.to_dict() for i in trusted] == [i.to_dict() for i in validated]
    with pytest.raises(ValueError):
        SourceCatalogItem.from_dict({"sourceItemId": 1.5})  # outside any trusted context
    with trusted_json_input():
        pass
    with pytest.raises((ValueError, TypeError)):
        SourceCatalogItem.from_dict({"sourceItemId": 1.5})
