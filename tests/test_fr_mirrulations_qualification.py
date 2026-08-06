from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import tools.fr_mirrulations_qualification as runner
import tools.fr_mirrulations_support as qualification
from docspec.adapters.content_fetchers import AnonymousS3ContentFetcher, RoutingContentFetcher
from docspec.adapters.source_catalog import LocalJsonlSourceCatalog
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import canonical_json_file_bytes, sha256_digest, stable_urn
from docspec.domain.references import ArtifactRef, DocumentReleaseRef
from docspec.errors import IntegrityError


def _write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")


def _fixture_inputs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> qualification.CorpusInputs:
    monkeypatch.setattr(qualification, "FULL_DOCUMENT_COUNT", 2)
    monkeypatch.setattr(qualification, "FEDERAL_REGISTER_COUNT", 1)
    monkeypatch.setattr(qualification, "MIRRULATIONS_COUNT", 1)
    fr_root = tmp_path / "fr"
    content = fr_root / "cache-xml/documents"
    receipts = fr_root / "cache-xml/receipts"
    content.mkdir(parents=True)
    receipts.mkdir(parents=True)
    source = b"<ROOT><P>Federal Register fixture.</P></ROOT>"
    digest = hashlib.sha256(source).hexdigest()
    (content / "2026-00001.xml").write_bytes(source)
    _write_json(
        receipts / "2026-00001.json",
        {
            "cache_file": "2026-00001.xml",
            "document_number": "2026-00001",
            "etag": None,
            "last_modified": "Wed, 05 Aug 2026 12:00:00 GMT",
            "resolved_url": "https://www.federalregister.gov/2026-00001.xml",
            "retrieved_on": "2026-08-05T12:00:00Z",
            "source_bytes": len(source),
            "source_sha256": digest,
            "source_url": "https://www.federalregister.gov/2026-00001.xml",
            "status": "ok",
        },
    )
    fr_draw = fr_root / "draw-manifest-final.json"
    _write_json(
        fr_draw,
        {
            "schema_version": qualification._FR_SCHEMA,
            "documents": [
                {"document_number": "2026-00001", "pages": 1, "title": "retained"},
                {"document_number": "2026-00002", "pages": 1, "title": "missing"},
            ],
        },
    )
    metadata = {
        "key": f"{qualification.MIRRULATIONS_PREFIX}0/documents/DOC-1.json",
        "size": 12,
        "etag": '"metadata"',
        "last_modified": "2026-08-05T12:00:00Z",
    }
    rendition = {
        "key": f"{qualification.MIRRULATIONS_PREFIX}0/documents/DOC-1_content.html",
        "size": 34,
        "etag": '"rendition"',
        "last_modified": "2026-08-05T12:00:01Z",
    }
    mirr_draw = tmp_path / "mirrulations-draw.json"
    draw = {
        "schema_version": qualification.MIRRULATIONS_SCHEMA,
        "source": {
            "bucket": qualification.MIRRULATIONS_BUCKET,
            "prefix": qualification.MIRRULATIONS_PREFIX,
        },
        "selection": {"max_documents": 1},
        "counts": {"selected_documents": 1},
        "documents": [
            {
                "document_id": "DOC-1",
                "json_revision": 0,
                "metadata_object": metadata,
                "rendition_object": rendition,
                "mirror_directory": f"{qualification.MIRRULATIONS_PREFIX}0/documents",
            }
        ],
    }
    draw["draw_id"] = qualification._mirrulations_draw_id(draw)
    _write_json(mirr_draw, draw)
    return qualification.CorpusInputs(fr_draw, receipts, content, mirr_draw)


def test_real_world_translation_preserves_exact_mappings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _fixture_inputs(tmp_path, monkeypatch)
    federal_register = qualification.validate_federal_register(inputs)
    mirrulations = qualification.validate_mirrulations_draw(inputs)

    assert len(federal_register.items) == 1
    assert federal_register.items[0].candidates[0].expected_digest == federal_register.items[0].version
    assert federal_register.items[0].candidates[0].locator == "2026-00001.xml"
    assert [candidate.candidate_id for candidate in mirrulations.items[0].candidates] == [
        "metadata-json",
        "rendition-html",
    ]
    assert all(candidate.locator.startswith("s3://mirrulations/") for candidate in mirrulations.items[0].candidates)


def test_translation_rejects_changed_federal_register_bytes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _fixture_inputs(tmp_path, monkeypatch)
    (inputs.federal_register_content / "2026-00001.xml").write_bytes(b"changed")

    with pytest.raises(IntegrityError, match="differs from its receipt"):
        qualification.validate_federal_register(inputs)


def test_translation_rejects_missing_mirrulations_pair(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _fixture_inputs(tmp_path, monkeypatch)
    draw = json.loads(inputs.mirrulations_draw.read_text(encoding="utf-8"))
    draw["documents"][0].pop("rendition_object")
    draw["draw_id"] = qualification._mirrulations_draw_id(draw)
    _write_json(inputs.mirrulations_draw, draw)

    with pytest.raises(IntegrityError, match="rendition object"):
        qualification.validate_mirrulations_draw(inputs)


def test_catalog_build_is_byte_identical_and_balanced(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _fixture_inputs(tmp_path, monkeypatch)
    tier = qualification.QualificationTier("fixture", 1, 1)
    monkeypatch.setattr(qualification, "TIERS", (tier,))
    first, first_summary = qualification.build_catalogs(inputs, catalog_root=tmp_path / "catalogs")
    second, second_summary = qualification.build_catalogs(inputs, catalog_root=tmp_path / "catalogs")

    assert first == second
    assert first_summary == second_summary
    assert first_summary["tiers"]["fixture"]["counts"]["documents"] == 2
    assert first_summary["tiers"]["fixture"]["counts"]["candidates"] == 3


def _passed_pytest_evidence(selectors: tuple[str, ...] = ()) -> dict[str, object]:
    collected = max(1, len(selectors))
    return {
        "command": qualification._pytest_evidence_command(selectors),
        "collected": collected,
        "passed": collected,
        "failed": 0,
        "errors": 0,
        "skipped": 0,
        "exitCode": 0,
        "verdict": "passed",
    }


def _passed_gate_results() -> dict[str, dict[str, object]]:
    return {
        gate_id: _passed_pytest_evidence(selectors)
        for gate_id, selectors in qualification.QUALIFICATION_GATE_SELECTORS.items()
    }


def _fixture_gate_receipt(repository: Path) -> dict[str, object]:
    return qualification._seal_gate_receipt(
        repository=repository,
        evidence_files=qualification._gate_evidence_files(repository),
        lint_result={
            "command": ["uv", "run", "ruff", "check", "."],
            "exitCode": 0,
            "verdict": "passed",
        },
        test_result=_passed_pytest_evidence(),
        gate_results=_passed_gate_results(),
    )


def test_gate_receipt_seals_the_tested_repository_state(tmp_path: Path) -> None:
    source = tmp_path / "tests/test_example.py"
    source.parent.mkdir(parents=True)
    source.write_text("def test_example():\n    assert True\n", encoding="utf-8")
    receipt = _fixture_gate_receipt(tmp_path)
    path = tmp_path / "output/gate.json"
    path.parent.mkdir()
    path.write_bytes(canonical_json_file_bytes(receipt))

    qualification.validate_gate_receipt(path, repository=tmp_path)
    source.write_text("def test_example():\n    assert False\n", encoding="utf-8")
    with pytest.raises(IntegrityError, match="evidence differs"):
        qualification.validate_gate_receipt(path, repository=tmp_path)


def test_gate_runner_rejects_source_change_during_execution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    before = [{"path": "tests/test.py", "digest": sha256_digest(b"before"), "byteSize": 6}]
    after = [{"path": "tests/test.py", "digest": sha256_digest(b"after"), "byteSize": 5}]
    evidence_calls = iter((before, after))
    monkeypatch.setattr(qualification, "_gate_evidence_files", lambda _repository: next(evidence_calls))
    monkeypatch.setattr(
        qualification,
        "_run_lint_evidence",
        lambda _repository: {
            "command": ["uv", "run", "ruff", "check", "."],
            "exitCode": 0,
            "verdict": "passed",
        },
    )
    monkeypatch.setattr(
        qualification,
        "_run_pytest_evidence",
        lambda _repository, selectors=(): _passed_pytest_evidence(selectors),
    )

    with pytest.raises(IntegrityError, match="source set changed"):
        qualification.run_qualification_gates(repository=tmp_path)


def _producer_fixture(tmp_path: Path) -> tuple[SimpleNamespace, Path, Path, Path]:
    spicyregs = tmp_path / "spicy-regs"
    builder = spicyregs / runner.BUILDER_RELATIVE
    builder.parent.mkdir(parents=True)
    builder.write_bytes(b"builder")
    output = tmp_path / "output"
    draw = output / "producer/mirrulations-draw.json"
    draw.parent.mkdir(parents=True)
    draw.write_bytes(b"draw")
    args = SimpleNamespace(spicyregs=spicyregs, output=output)
    return args, builder, draw, output / "producer/spicyregs-validation.json"


def _producer_receipt(spicyregs: Path, builder: Path, draw: Path, **overrides: object) -> dict[str, object]:
    content: dict[str, object] = {
        "format": "docspec-qualification-producer-validation",
        "formatVersion": "1.0",
        "campaignId": qualification.CAMPAIGN_ID,
        "producer": "SpicyRegs",
        "repository": spicyregs.resolve().as_posix(),
        "commit": "current-commit",
        "builder": builder.resolve().as_posix(),
        "builderDigest": sha256_digest(builder.read_bytes()),
        "draw": draw.resolve().as_posix(),
        "drawDigest": sha256_digest(draw.read_bytes()),
        "validationOutput": "{}",
        "verdict": "passed",
    }
    content.update(overrides)
    return {**content, "producerReceiptId": stable_urn("qualification-producer-validation", content)}


def test_preexisting_mirrulations_draw_requires_a_producer_receipt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    args, _builder, _draw, _receipt = _producer_fixture(tmp_path)
    monkeypatch.setattr(runner, "_run_checked", lambda *_args, **_kwargs: "current-commit")

    with pytest.raises(IntegrityError, match="no producer provenance receipt"):
        runner.freeze_producer_inputs(args)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("commit", "different-commit"),
        ("builderDigest", "sha256:" + "1" * 64),
        ("drawDigest", "sha256:" + "2" * 64),
    ],
)
def test_preexisting_mirrulations_draw_rejects_mismatched_provenance(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    field: str,
    value: str,
) -> None:
    args, builder, draw, receipt_path = _producer_fixture(tmp_path)
    receipt = _producer_receipt(args.spicyregs, builder, draw, **{field: value})
    receipt_path.write_bytes(canonical_json_file_bytes(receipt))
    monkeypatch.setattr(runner, "_run_checked", lambda *_args, **_kwargs: "current-commit")

    with pytest.raises(IntegrityError, match="provenance differs"):
        runner.freeze_producer_inputs(args)


def _gate_summary() -> dict[str, object]:
    gate_results = _passed_gate_results()
    return {
        "gateReceiptId": "urn:docspec:test:gate",
        "evidenceSourceSetDigest": sha256_digest(b"sources"),
        "requiredGates": list(qualification.REQUIRED_QUALIFICATION_GATES),
        "checks": {
            "lint": {
                "command": ["uv", "run", "ruff", "check", "."],
                "exitCode": 0,
                "verdict": "passed",
            },
            "tests": _passed_pytest_evidence(),
        },
        "gateResults": [
            {
                "gateId": gate_id,
                "selectors": list(qualification.QUALIFICATION_GATE_SELECTORS[gate_id]),
                "result": gate_results[gate_id],
            }
            for gate_id in qualification.REQUIRED_QUALIFICATION_GATES
        ],
        "verdict": "passed",
    }


def test_execution_manifest_reconstructs_and_rejects_drift(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inputs = _fixture_inputs(tmp_path, monkeypatch)
    repository = tmp_path / "repository"
    files = repository / "files"
    files.mkdir(parents=True)
    for name in ("plan.json", "request.json", "builder.py"):
        (files / name).write_text(name, encoding="utf-8")
    tools = repository / "tools"
    tools.mkdir()
    for name in ("runner.py", "support.py"):
        (tools / name).write_text(name, encoding="utf-8")
    outputs = {"sourceContent": inputs.federal_register_content, "records": tmp_path / "records"}
    outputs["records"].mkdir()
    federal_register, mirrulations = qualification.build_source_items(inputs)
    source_ref = SourceItem("item", "v1", (CandidateFile("c", "file", "text/plain"),))
    source_catalog_ref = LocalJsonlSourceCatalog(tmp_path / "catalogs").write((source_ref,))
    gate_receipt = _fixture_gate_receipt(repository)
    gate_path = repository / "output/gate.json"
    gate_path.parent.mkdir()
    gate_path.write_bytes(canonical_json_file_bytes(gate_receipt))
    manifest = qualification.build_execution_manifest(
        tier=qualification.QualificationTier("smoke", 1, 0),
        inputs=inputs,
        federal_register=federal_register,
        mirrulations=mirrulations,
        source_catalog=source_catalog_ref,
        processing_plan_path=files / "plan.json",
        processing_plan_id="plan-id",
        run_request_path=files / "request.json",
        output_roots=outputs,
        spicyregs_repository=repository,
        spicyregs_commit="abc123",
        spicyregs_builder=files / "builder.py",
        runner_path=tools / "runner.py",
        runner_support_path=tools / "support.py",
        gate_receipt_path=gate_path,
        workers=2,
        max_object_bytes=1024,
        retry_policy={"maxAttempts": 1},
    )
    path = repository / "output/manifest.json"
    path.write_bytes(canonical_json_file_bytes(manifest))

    validated = qualification.validate_execution_manifest(path)
    fetcher = qualification.reconstruct_fetcher(validated, identity_only=True)
    assert isinstance(fetcher, RoutingContentFetcher)
    assert fetcher.s3.downloader_id == AnonymousS3ContentFetcher.downloader_id

    federal_register_path = inputs.federal_register_content / "2026-00001.xml"
    federal_register_bytes = federal_register_path.read_bytes()
    federal_register_path.write_bytes(b"changed")
    with pytest.raises(IntegrityError, match="differs from its receipt"):
        qualification.validate_execution_manifest(path)
    federal_register_path.write_bytes(federal_register_bytes)

    (files / "builder.py").write_text("changed", encoding="utf-8")
    with pytest.raises(IntegrityError, match="builder differs"):
        qualification.validate_execution_manifest(path)


def test_tier_run_delegates_restart_detection_to_durable_docspec_state(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tier_root = tmp_path / "runs/smoke"
    tier_root.mkdir(parents=True)
    manifest = {
        "qualificationGates": {"path": (tmp_path / "gate.json").as_posix()},
        "runRequest": {"path": (tmp_path / "run-request.json").as_posix()},
    }
    sentinel_fetcher = object()
    observed: dict[str, object] = {}

    monkeypatch.setattr(runner, "_require_predecessor", lambda *_args: None)
    monkeypatch.setattr(runner, "validate_execution_manifest", lambda path: manifest)
    monkeypatch.setattr(runner, "validate_gate_receipt", lambda *_args, **_kwargs: {})
    monkeypatch.setattr(runner, "reconstruct_fetcher", lambda _manifest: sentinel_fetcher)
    monkeypatch.setattr(runner, "_content_fetcher_composition", lambda *_args: {})
    monkeypatch.setattr(runner, "_local_run_request", lambda _path: {"request": "fixture"})

    class _StopAfterRunDispatch(Exception):
        pass

    def observe_run(request: dict[str, object], **kwargs: object) -> ArtifactRef:
        observed["request"] = request
        observed.update(kwargs)
        raise _StopAfterRunDispatch

    monkeypatch.setattr(runner, "_execute_local_run", observe_run)
    args = SimpleNamespace(output=tmp_path, workers=2)

    with pytest.raises(_StopAfterRunDispatch):
        runner.run_tier(args, "smoke")

    assert observed["resume"] is None
    assert observed["content_fetcher"] is sentinel_fetcher


class _FakeDocumentCatalog:
    def __init__(self, rows: dict[str, list[dict[str, object]]]) -> None:
        self.rows = rows

    def open_reader(self, _reference: DocumentReleaseRef) -> object:
        catalog = self

        class _Reader:
            release = SimpleNamespace(logical_state_digest="sha256:" + "0" * 64)

            def scan(self, *, layer_kind: str):
                yield from catalog.rows.get(layer_kind, [])

        return _Reader()

    def scan(self, _reference: DocumentReleaseRef, *, layer_kind: str):
        yield from self.rows.get(layer_kind, [])


class _FakeControls:
    def __init__(self, values: dict[str, dict[str, object]] | None = None) -> None:
        self.values = values or {}

    def load(self, reference: ArtifactRef) -> dict[str, object]:
        return self.values[reference.artifact_id]


def test_candidate_census_classifies_not_attempted_after_failure(tmp_path: Path) -> None:
    item = SourceItem(
        "item",
        "v1",
        (
            CandidateFile("json", "a.json", "application/json", expected_size=10),
            CandidateFile("html", "a.html", "text/html", expected_size=20),
        ),
    )
    source_catalog = LocalJsonlSourceCatalog(tmp_path / "catalogs")
    source_ref = source_catalog.write((item,))
    release_ref = DocumentReleaseRef("release", "release.json", "sha256:" + "0" * 64)
    catalog = _FakeDocumentCatalog(
        {
            "files": [],
            "representations": [],
            "segments": [],
            "dispositions": [
                {"sourceItemId": "item", "payload": {"disposition": "rejected-run"}},
            ],
        }
    )

    census = qualification.build_candidate_census(
        tier="fixture",
        source_catalog=source_catalog,
        source_catalog_ref=source_ref,
        document_catalog=catalog,
        controls=_FakeControls(),
        release_ref=release_ref,
        gate_receipt=_gate_summary(),
    )

    assert census["counts"]["candidateStatuses"] == {
        "acquisition-failed": 1,
        "not-attempted": 1,
        "processed": 0,
        "processing-failed": 0,
    }
    assert census["verdict"] == "failed"


def test_candidate_census_accepts_a_verified_zero_segment_representation(tmp_path: Path) -> None:
    item = SourceItem(
        "item",
        "v1",
        (CandidateFile("text", "empty.txt", "text/plain", expected_size=0),),
    )
    source_catalog = LocalJsonlSourceCatalog(tmp_path / "catalogs")
    source_ref = source_catalog.write((item,))
    release_ref = DocumentReleaseRef("release", "release.json", "sha256:" + "0" * 64)
    receipt = qualification.SegmentationReceipt(
        "representation",
        "docspec.paragraph/v1",
        (),
    ).to_dict()
    receipt_ref = ArtifactRef(
        "receipt",
        "receipt.json",
        sha256_digest(canonical_json_file_bytes(receipt)),
        "application/json",
        len(canonical_json_file_bytes(receipt)),
    )
    catalog = _FakeDocumentCatalog(
        {
            "files": [
                {
                    "sourceItemId": "item",
                    "payload": {"candidateId": "text", "fileId": "file"},
                }
            ],
            "representations": [
                {
                    "sourceItemId": "item",
                    "payload": {"fileId": "file", "representationId": "representation"},
                }
            ],
            "segments": [],
            "receipts": [
                {
                    "sourceItemId": "item",
                    "payload": {"artifact": receipt_ref.to_dict()},
                }
            ],
            "dispositions": [
                {"sourceItemId": "item", "payload": {"disposition": "captured"}},
            ],
        }
    )

    census = qualification.build_candidate_census(
        tier="fixture",
        source_catalog=source_catalog,
        source_catalog_ref=source_ref,
        document_catalog=catalog,
        controls=_FakeControls({"receipt": receipt}),
        release_ref=release_ref,
        gate_receipt=_gate_summary(),
    )

    assert census["counts"]["candidateStatuses"] == {
        "acquisition-failed": 0,
        "not-attempted": 0,
        "processed": 1,
        "processing-failed": 0,
    }
    assert census["verdict"] == "passed"
