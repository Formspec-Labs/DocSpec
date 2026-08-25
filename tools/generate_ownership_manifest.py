"""Generate ownership/modules.json from the source tree plus hand-authored metadata.

ownership/modules.json used to hand-list all 67 `src/docspec` modules, each with a
literal `"owner": "DocSpec"` -- 67 copies of a constant, since this is a
single-owner project -- and no generator: adding a module meant hand-editing
both the new .py file and a new JSON object, with nothing to catch a stale or
missing entry until tests/test_machine_files.py's `declared == actual` path-set
assertion ran.

This generator makes the module *path list* mechanically derived: it walks
`src/docspec` for the real, current `*.py` files (the same source the old
test verified against) and looks up each one's hand-authored `capability`,
`status`, and `conformanceTests` in `_MODULE_METADATA` below -- the one
place left to edit by hand, and only for what a generator cannot invent
(what a module does, and which conformance categories it backs). Both
directions fail loudly: a module on disk with no metadata entry, or a
metadata entry for a module no longer on disk, raises instead of silently
producing a stale or incomplete manifest. `owner` is emitted once at the top
level instead of once per module.

Usage:
    uv run python -m tools.generate_ownership_manifest > ownership/modules.json

tests/test_machine_files.py imports `build_manifest` and asserts the checked-in
file is exactly what this module currently produces.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

FORMAT = "docspec-module-ownership"
FORMAT_VERSION = "2.0"
OWNER = "DocSpec"
SOURCE_ROOT = "src/docspec"
ARCHIVE_ROOT = "archive/legacy-2026-08-05"

# path -> (capability, status, conformanceTests). The one hand-maintained input:
# a generator cannot invent what a module does or which conformance categories
# it backs. Everything else in the manifest is derived.
_MODULE_METADATA: dict[str, tuple[str, str, tuple[str, ...]]] = {
    "src/docspec/__init__.py": ("Public package identity, version, and stable base error", "implemented", ("CORE-INSTALL",)),
    "src/docspec/adapters/__init__.py": ("Public local-adapter selection surface", "implemented", ("BOUNDARY-IMPORT",)),
    "src/docspec/adapters/content_fetchers.py": (
        "Closeable contained local, allowlisted HTTPS, and anonymous-S3 source acquisition behind a sealed routing identity",
        "implemented",
        ("ACQUISITION", "RECOVERY"),
    ),
    "src/docspec/adapters/dagster.py": ("Lazy optional Dagster dynamic-task mapping with one coordinator membership proof, indexed per-task checks, and subprocess workers", "partial", ("SCALE", "SCHEDULER-PORTABILITY")),
    "src/docspec/adapters/execution.py": ("Bounded local execution and scheduler-neutral serialized external dispatch", "partial", ("SCHEDULER-PORTABILITY", "RECOVERY")),
    "src/docspec/adapters/platform_artifact.py": (
        "Rulespec-backed derivation identity, bounded member sealing, and DocSpec semantic verification",
        "implemented",
        ("DOCUMENT-CATALOG-CONTRACT", "DOCUMENT-RELEASE-INTEGRITY", "RELEASE-MANIFEST", "SCHEDULER-PORTABILITY"),
    ),
    "src/docspec/adapters/processor_cache.py": ("Disposable local SQLite lookup from exact reuse keys to immutable processor results", "implemented", ("PROCESSOR-CONTRACT", "RECOVERY")),
    "src/docspec/adapters/reconciliation.py": ("Bounded ephemeral SQLite assembly for one reconciled run", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "INCREMENTAL-EQUIVALENCE", "RECOVERY")),
    "src/docspec/adapters/s3_blob.py": ("Lazy optional Amazon S3 and S3-compatible immutable blob storage", "implemented", ("AMAZON-S3-BLOB-STORE", "BLOB-STORE-CONTRACT", "S3-COMPATIBLE-BLOB-STORE")),
    "src/docspec/adapters/sinks.py": ("Returned-result, durable-dataset, and hybrid result delivery", "implemented", ("RESULT-SINK", "RECOVERY")),
    "src/docspec/adapters/storage.py": ("Portable local blobs, control records, job revisions, record layers, and shared-derivation release catalog", "implemented", ("BLOB-STORE-CONTRACT", "DOCUMENT-CATALOG-CONTRACT", "DOCUMENT-STORE", "DOCUMENT-RELEASE-INTEGRITY", "INCREMENTAL-EQUIVALENCE", "RECORD-STORAGE-CONTRACT", "RELEASE-MANIFEST")),
    "src/docspec/application/__init__.py": ("Public scheduler-neutral application-service and release-verifier surface", "implemented", ("BOUNDARY-IMPORT", "DOCUMENT-RELEASE-INTEGRITY")),
    "src/docspec/application/commit.py": ("Typed release coherence verification, computed rollups, conditional commit, and catalog activation", "implemented", ("DOCUMENT-CATALOG-CONTRACT", "DOCUMENT-RELEASE-INTEGRITY", "RELEASE-MANIFEST")),
    "src/docspec/application/delivery.py": ("Idempotent bounded job delivery through an injected result sink", "implemented", ("RESULT-SINK", "RECOVERY")),
    "src/docspec/application/execution.py": ("Receipt-derived candidate-level and stage-level resume for acquisition, extraction, segmentation, and injected processor layers in bounded jobs", "implemented", ("ACQUISITION", "DOCUMENT-STORE", "EVIDENCE-ROUNDTRIP", "PROCESSOR-CONTRACT", "RECOVERY", "REPRESENTATION", "SEGMENTATION")),
    "src/docspec/application/maintenance.py": ("Format-neutral release compaction and verified reachability-based blob retention", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "INCREMENTAL-EQUIVALENCE")),
    "src/docspec/application/planner.py": ("Streaming precompiled source, partition, and logical-bucket selection with deterministic bounded-job planning", "implemented", ("ACQUISITION", "DOCUMENT-STORE", "INCREMENTAL-EQUIVALENCE", "SOURCE-CATALOG-CONTRACT")),
    "src/docspec/application/reconcile.py": ("Complete store reconciliation into immutable active logical layers and run receipts", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "INCREMENTAL-EQUIVALENCE", "RECOVERY")),
    "src/docspec/application/service.py": ("Five-function small-reference application facade for local or external schedulers", "implemented", ("SCHEDULER-PORTABILITY",)),
    "src/docspec/application/store_state.py": ("Shared verified latest-revision recovery for stale scheduler references", "implemented", ("RECOVERY", "SCHEDULER-PORTABILITY")),
    "src/docspec/application/work_budget.py": ("Aggregate actual source, page or frame, segment, processor, memory, and duration limits", "implemented", ("ACQUISITION", "DOCUMENT-STORE", "PROCESSOR-CONTRACT", "RECOVERY", "SEGMENTATION")),
    "src/docspec/cli.py": ("Single operator command for the standalone lifecycle, release compaction, and bounded retention inspection", "implemented", ("CORE-INSTALL", "PACKAGE-RELEASE")),
    "src/docspec/conformance/__init__.py": ("Public executable conformance evidence and shared-fixture surface", "implemented", ("PACKAGE-RELEASE", "SOURCE-CATALOG-CONTRACT")),
    "src/docspec/conformance/fixtures.py": ("Closed byte-exact conformance fixture verification and materialization", "implemented", ("PACKAGE-RELEASE", "SOURCE-CATALOG-CONTRACT")),
    "src/docspec/conformance/runner.py": ("Fail-closed required-test execution and sealed conformance reports", "implemented", ("PACKAGE-RELEASE",)),
    "src/docspec/domain/__init__.py": ("Public immutable domain-record surface", "implemented", ("BOUNDARY-IMPORT",)),
    "src/docspec/domain/content.py": ("Source item, exact file, persisted evidence mapping, representation, segment-coordinate, and derived-output records", "implemented", ("ACQUISITION", "DOCUMENT-RELEASE-INTEGRITY", "EVIDENCE-ROUNDTRIP", "PROCESSOR-CONTRACT", "REPRESENTATION", "SEGMENTATION")),
    "src/docspec/domain/delivery.py": ("Stable idempotent delivery records and bounded logical release-lineage verification", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "EVIDENCE-ROUNDTRIP", "RESULT-SINK")),
    "src/docspec/domain/execution.py": ("Closed execution profiles, handoff roots, and bounded scheduler task and result messages", "partial", ("DOCUMENT-STORE", "RECOVERY", "SCHEDULER-PORTABILITY")),
    "src/docspec/domain/identity.py": ("Canonical JSON, immutable values, SHA-256 verification, and stable identities", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "EVIDENCE-ROUNDTRIP", "RELEASE-MANIFEST")),
    "src/docspec/domain/jobs.py": ("Bounded immutable DocumentStore revisions, entries, failures, and state transitions", "implemented", ("DOCUMENT-STORE", "RECOVERY")),
    "src/docspec/domain/maintenance.py": ("Closed blob-retention roots and logical-equivalence compaction receipts", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "INCREMENTAL-EQUIVALENCE")),
    "src/docspec/domain/plans.py": ("Pinned processing plans, stage policies, and bounded work limits", "implemented", ("DOCUMENT-STORE", "INCREMENTAL-EQUIVALENCE", "PROFILE-COMPATIBILITY")),
    "src/docspec/domain/policies.py": ("Closed retention, data-use, provider-evidence, retry, and accepted-failure policies", "implemented", ("PROCESSOR-CONTRACT", "RECOVERY")),
    "src/docspec/domain/processors.py": ("Pinned processor descriptions, dependency graph, invalidation, and closed request and result records", "implemented", ("INCREMENTAL-EQUIVALENCE", "PROCESSOR-CONTRACT")),
    "src/docspec/domain/profiles.py": ("Format-neutral profile descriptions, pins, roles, and selected profile sets", "implemented", ("PROFILE-COMPATIBILITY", "PROFILE-DESCRIPTION")),
    "src/docspec/domain/receipts.py": ("Delivery, run, and catalog-commit receipt records with bounded ledger references", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "DOCUMENT-STORE", "RECOVERY", "RESULT-SINK")),
    "src/docspec/domain/references.py": ("Immutable artifact, blob, layer, catalog, job, and release references", "implemented", ("BLOB-STORE-CONTRACT", "DOCUMENT-CATALOG-CONTRACT", "DOCUMENT-RELEASE-INTEGRITY")),
    "src/docspec/domain/release.py": ("Complete-current-state semantic view of one shared derivation", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "INCREMENTAL-EQUIVALENCE", "RELEASE-MANIFEST")),
    "src/docspec/domain/scale.py": ("Closed content-addressed scale campaign inputs, resources, policies, targets, and acceptance authority", "implemented", ("SCALE",)),
    "src/docspec/domain/security.py": ("Credential detection, receipt rejection, and diagnostic redaction", "implemented", ("PROCESSOR-CONTRACT",)),
    "src/docspec/domain/storage.py": ("Format-neutral logical schemas, stable partitions, and partition identities", "implemented", ("INCREMENTAL-EQUIVALENCE", "RECORD-STORAGE-CONTRACT")),
    "src/docspec/errors.py": ("Stable fail-closed exception hierarchy", "implemented", ("BOUNDARY-IMPORT",)),
    "src/docspec/ports/__init__.py": ("Public dependency-inversion interface surface", "implemented", ("BOUNDARY-IMPORT",)),
    "src/docspec/ports/blob_store.py": ("Provider-neutral immutable whole-file byte storage", "implemented", ("BLOB-STORE-CONTRACT",)),
    "src/docspec/ports/content_fetcher.py": ("Replaceable streamed content acquisition and transport metadata", "implemented", ("ACQUISITION",)),
    "src/docspec/ports/control_repository.py": ("Small immutable control-plane artifact persistence", "implemented", ("RECOVERY",)),
    "src/docspec/ports/document_catalog.py": ("Versioned corpus open, compare, stage, and conditional commit boundary", "implemented", ("DOCUMENT-CATALOG-CONTRACT", "DOCUMENT-RELEASE-INTEGRITY")),
    "src/docspec/ports/document_store_repository.py": ("Immutable DocumentStore revision and checkpoint persistence", "implemented", ("DOCUMENT-STORE", "RECOVERY")),
    "src/docspec/ports/execution_backend.py": ("Scheduler-neutral execution of small immutable job references", "implemented", ("SCHEDULER-PORTABILITY",)),
    "src/docspec/ports/extractor.py": ("Replaceable representation extraction boundary", "implemented", ("REPRESENTATION",)),
    "src/docspec/ports/processor.py": ("Injected content-processor boundary", "implemented", ("PROCESSOR-CONTRACT",)),
    "src/docspec/ports/processor_cache.py": ("Replaceable exact processor-result lookup boundary", "implemented", ("PROCESSOR-CONTRACT", "RECOVERY")),
    "src/docspec/ports/profile_state_reachability.py": ("Injected traversal from physical profile-state roots to retained immutable blobs", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "INCREMENTAL-EQUIVALENCE")),
    "src/docspec/ports/reconciliation_workspace.py": ("Provider-neutral bounded reconciliation workspace boundary", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "INCREMENTAL-EQUIVALENCE", "RECOVERY")),
    "src/docspec/ports/record_storage.py": ("Format-neutral partitioned logical-record storage and policy-introspection boundary", "implemented", ("DOCUMENT-RELEASE-INTEGRITY", "RECORD-STORAGE-CONTRACT")),
    "src/docspec/ports/record_workspace.py": ("Format-neutral bounded scratch-record workspace shared by planning and reconciliation", "implemented", ("ACQUISITION", "INCREMENTAL-EQUIVALENCE", "RECORD-STORAGE-CONTRACT")),
    "src/docspec/ports/result_sink.py": ("Durable, returned-result, or hybrid delivery boundary", "implemented", ("RESULT-SINK",)),
    "src/docspec/ports/segmenter.py": ("Replaceable source-grounded segmentation boundary", "implemented", ("EVIDENCE-ROUNDTRIP", "SEGMENTATION")),
    "src/docspec/ports/source_catalog.py": ("Sealed source-catalog snapshot and change-set reader boundary", "implemented", ("SOURCE-CATALOG-CONTRACT",)),
    "src/docspec/processing/__init__.py": ("Public deterministic content-processing implementation surface", "implemented", ("REPRESENTATION", "SEGMENTATION")),
    "src/docspec/processing/artifacts.py": ("Worker-local bytes joined to persisted representation mappings and segment coordinates with reversible evidence", "implemented", ("EVIDENCE-ROUNDTRIP", "REPRESENTATION", "SEGMENTATION")),
    "src/docspec/processing/extraction.py": ("Deterministic text, HTML, XML, JSON, image, and lazy PDF extraction with strict versioned receipts", "implemented", ("REPRESENTATION",)),
    "src/docspec/processing/json_tools.py": ("Strict JSON decoding and exact record-slice coordinates", "implemented", ("EVIDENCE-ROUNDTRIP", "REPRESENTATION", "SEGMENTATION")),
    "src/docspec/processing/processors.py": ("Deterministic injected statistics processor and evidence-linked output", "implemented", ("PROCESSOR-CONTRACT",)),
    "src/docspec/processing/segmentation.py": ("Paragraph, page, record, and whole-image source-grounded segmentation with strict versioned receipts", "implemented", ("EVIDENCE-ROUNDTRIP", "SEGMENTATION")),
    "src/docspec/profile_registry.py": ("Closed profile loading, inventory, dependency verification, and one-per-role selection", "implemented", ("PROFILE-COMPATIBILITY", "PROFILE-DESCRIPTION")),
}


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def _real_module_paths(repo_root: Path) -> list[str]:
    source_root = repo_root / SOURCE_ROOT
    return sorted(path.relative_to(repo_root).as_posix() for path in source_root.rglob("*.py"))


def build_manifest(repo_root: Path | None = None) -> dict[str, Any]:
    root = repo_root if repo_root is not None else _repo_root()
    real_paths = _real_module_paths(root)

    missing_metadata = sorted(set(real_paths) - set(_MODULE_METADATA))
    if missing_metadata:
        raise KeyError(
            "no ownership metadata for module(s), add an entry to "
            f"_MODULE_METADATA in {__name__}: {missing_metadata}"
        )
    stale_metadata = sorted(set(_MODULE_METADATA) - set(real_paths))
    if stale_metadata:
        raise KeyError(
            f"_MODULE_METADATA in {__name__} names module(s) no longer on disk, remove "
            f"the entry: {stale_metadata}"
        )

    modules = [
        {
            "path": path,
            "capability": _MODULE_METADATA[path][0],
            "status": _MODULE_METADATA[path][1],
            "conformanceTests": list(_MODULE_METADATA[path][2]),
        }
        for path in real_paths
    ]
    return {
        "format": FORMAT,
        "formatVersion": FORMAT_VERSION,
        "implementationStatus": "standalone-reference-implementation",
        "inventoryBasis": "working-tree",
        "sourceRoot": SOURCE_ROOT,
        "archiveRoot": ARCHIVE_ROOT,
        "owner": OWNER,
        "modules": modules,
    }


def manifest_bytes(repo_root: Path | None = None) -> bytes:
    return (json.dumps(build_manifest(repo_root), indent=2) + "\n").encode("utf-8")


def main() -> None:
    import sys

    sys.stdout.buffer.write(manifest_bytes())


if __name__ == "__main__":
    main()
