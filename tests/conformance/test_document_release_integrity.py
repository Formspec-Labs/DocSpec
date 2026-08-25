from __future__ import annotations

import importlib
import json
import sys
from pathlib import Path

import pytest

from docspec.adapters.content_fetchers import LocalFileContentFetcher
from docspec.domain.content import CapturedFile, Representation, Segment
from docspec.domain.policies import AcceptedFailurePolicy, RetryPolicy
from docspec.domain.receipts import RunReceipt
from docspec.domain.references import DocumentReleaseRef, StoreRef
from docspec.domain.content import SourceItem
from docspec.errors import DocSpecError, IntegrityError

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_equivalence = importlib.import_module("tests.conformance.test_incremental_equivalence")
_pipeline_helpers = importlib.import_module("tests.test_application_pipeline")
_processor_helpers = importlib.import_module("tests.test_processor_reprocessing")
_platform = _equivalence._platform
_run = _pipeline_helpers._run
_write_source = _pipeline_helpers._write_source
_CountingProcessor = _processor_helpers._CountingProcessor
_description = _processor_helpers._description
_plan = _processor_helpers._plan


def _committed_release(root: Path):
    """Run the complete real pipeline once and return the platform, the
    committed reference, and the opened release."""

    retry = RetryPolicy(base_delay_milliseconds=0)
    accepted = AcceptedFailurePolicy()
    platform = _platform(root, member_bytes=1024 * 1024)
    first = _write_source(platform.sources / "a.txt", "Alpha retains one exact paragraph.")
    second = _write_source(platform.sources / "b.txt", "Bravo retains another exact paragraph.")
    source = platform.source_catalog.write(
        (
            SourceItem("document-a", "v1", (first,), metadata={"expectedSegments": 1}),
            SourceItem("document-b", "v1", (second,), metadata={"expectedSegments": 1}),
        )
    )
    processor = _CountingProcessor(_description("integrity", "1", retry))
    plan = _plan(source, None, (processor,), retry, accepted)
    _, _, _, _, reference = _run(
        plan=plan,
        source_catalog=platform.source_catalog,
        controls=platform.controls,
        stores=platform.stores,
        blobs=platform.blobs,
        records=platform.records,
        catalog=platform.catalog,
        fetcher=LocalFileContentFetcher(platform.sources),
        processors=(processor,),
        partition_policy=platform.partition_policy,
    )
    return platform, reference, platform.catalog.open(reference)


def _layer_paths(records_root: Path, state_ref: str) -> set[Path]:
    state_path = records_root / state_ref
    members = json.loads(state_path.read_text(encoding="utf-8"))["members"]
    return {state_path, *(records_root / member["path"] for member in members)}


def _retained_object_paths(platform, reference: DocumentReleaseRef, release) -> dict[str, set[Path]]:
    """Enumerate every persisted object the release's reference graph names."""

    categories: dict[str, set[Path]] = {
        "release-root": {platform.catalog.root / reference.locator},
        "controls": set(),
        "layers": set(),
        "stores": set(),
        "blobs": set(),
    }
    for artifact in (release.processing_plan, release.run_receipt, release.catalog_commit_receipt):
        categories["controls"].add(platform.controls.root / artifact.locator)
    run = RunReceipt.from_dict(platform.controls.load(release.run_receipt))
    for artifact in (run.execution_profile, run.execution_handoff):
        categories["controls"].add(platform.controls.root / artifact.locator)
    for layer in (
        *release.active_layers,
        run.store_ledger,
        run.selection_ledger,
        run.task_result_ledger,
    ):
        categories["layers"].update(_layer_paths(platform.records.root, layer.state_ref))
    planned_state = platform.stores.root / run.planned_store_ledger.state_ref
    planned_member = json.loads(planned_state.read_text(encoding="utf-8"))["member"]["path"]
    categories["stores"].update({planned_state, platform.stores.root / planned_member})
    for row in platform.records.stream(run.store_ledger):
        categories["stores"].add(platform.stores.root / StoreRef.from_dict(row["store"]).locator)
    reader = platform.catalog.open_reader(reference)
    for row in reader.scan(layer_kind="files"):
        categories["blobs"].add(platform.blobs.root / CapturedFile.from_dict(row["payload"]).blob.locator)
    for row in reader.scan(layer_kind="representations"):
        categories["blobs"].add(platform.blobs.root / Representation.from_dict(row["payload"]).blob.locator)
    for row in reader.scan(layer_kind="segments"):
        categories["blobs"].add(platform.blobs.root / Segment.from_dict(row["payload"]).content.locator)
    return categories


def test_composed_verification_covers_every_retained_authoritative_object(tmp_path: Path) -> None:
    """Flip bytes in each persisted object the release references -- root,
    control artifacts, layer state and members, sealed stores, and retained
    blobs -- and require the composed open to fail closed every time."""

    platform, reference, release = _committed_release(tmp_path / "platform")
    assert release.blob_roots, "a release retaining content must declare its blob roots"
    categories = _retained_object_paths(platform, reference, release)
    for category, paths in categories.items():
        assert paths, f"the fixture release must retain at least one {category} object"
        for path in paths:
            assert path.is_file(), f"referenced {category} object is not a regular file: {path}"

    swept = 0
    for category in sorted(categories):
        for path in sorted(categories[category]):
            original = path.read_bytes()
            path.write_bytes(b"\x00" + original[1:])
            try:
                with pytest.raises(DocSpecError):
                    platform.catalog.open(reference)
            finally:
                path.write_bytes(original)
            swept += 1
    assert swept == sum(len(paths) for paths in categories.values())
    assert platform.catalog.open(reference) == release, "the sweep must leave the committed state intact"


def test_missing_retained_source_bytes_fail_the_composed_verification(tmp_path: Path) -> None:
    platform, reference, release = _committed_release(tmp_path / "platform")
    reader = platform.catalog.open_reader(reference)
    captured = CapturedFile.from_dict(next(iter(reader.scan(layer_kind="files")))["payload"])
    blob_path = platform.blobs.root / captured.blob.locator
    original = blob_path.read_bytes()

    blob_path.unlink()
    with pytest.raises(IntegrityError, match="blob"):
        platform.catalog.open(reference)

    blob_path.write_bytes(original)
    assert platform.catalog.open(reference) == release
