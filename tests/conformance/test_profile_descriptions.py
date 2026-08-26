from __future__ import annotations

import importlib
import json
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from docspec.cli import main
from docspec.domain.content import CandidateFile, SourceItem
from docspec.domain.identity import canonical_json_file_bytes, identity_digest, sha256_digest
from docspec.domain.plans import ProcessingPlan, StagePolicy, WorkLimits
from docspec.domain.policies import AcceptedFailurePolicy, DataUsePolicy, RetentionPolicy, RetryPolicy
from docspec.domain.processors import ProcessorSet
from docspec.domain.profiles import ProfileSet
from docspec.errors import ProfileError
from docspec.processing.extraction import DefaultExtractorRegistry
from docspec.processing.processors import ContentStatisticsProcessor
from docspec.processing.segmentation import DefaultSegmenterRegistry
from docspec.profile_registry import ProfileRegistry

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
_helpers = importlib.import_module("tests.helpers")
write_shared_source_catalog = _helpers.write_shared_source_catalog
_cli_helpers = importlib.import_module("tests.test_cli")
_portable_local_profiles = _cli_helpers._portable_local_profiles
_write_local_run_request = _cli_helpers._write_local_run_request

PROFILE_ROOT = ROOT / "profiles"
# Identity-bearing description fields: changing any one must change the
# description digest a plan pins, or a deployment could swap executable
# behavior under an existing pin.
_IDENTITY_FIELDS = (
    "capabilities",
    "compatibility",
    "configuration",
    "governancePolicies",
    "implementationId",
    "implementationModule",
    "limits",
    "logicalSchemas",
    "physicalMediaTypes",
    "version",
)


def _storage_profile_paths() -> list[Path]:
    paths = sorted(PROFILE_ROOT.glob("*.json"))
    assert paths
    return paths


def test_every_description_on_disk_is_closed_versioned_digest_pinned_and_capable(tmp_path: Path) -> None:
    governed = set(_storage_profile_paths())
    assert set(PROFILE_ROOT.rglob("*.json")) == governed, (
        "a profile description exists that no registered loader governs"
    )

    for path in _storage_profile_paths():
        registered = ProfileRegistry.from_file(path)
        description = registered.description
        major, minor, patch = description.version.split(".")
        assert all(component.isdigit() for component in (major, minor, patch))
        assert description.configuration_digest == identity_digest(description.configuration)
        assert description.capabilities == tuple(sorted(description.capabilities))
        assert description.capabilities and description.limits
        assert registered.description_digest.startswith("sha256:")

        value = json.loads(path.read_text(encoding="utf-8"))
        widened = tmp_path / f"widened-{path.name}"
        widened.write_text(json.dumps({**value, "undeclaredField": True}), encoding="utf-8")
        with pytest.raises(ProfileError, match="closed profile shape"):
            ProfileRegistry.from_file(widened)

        for field in _IDENTITY_FIELDS:
            assert field in value, f"{path.name} lost identity-bearing field {field}"

        drifted_value = json.loads(path.read_text(encoding="utf-8"))
        drifted_value["version"] = "999.0.0"
        drifted = tmp_path / f"drifted-{path.name}"
        drifted.write_text(json.dumps(drifted_value), encoding="utf-8")
        assert ProfileRegistry.from_file(drifted).description_digest != registered.description_digest

        evidence_value = json.loads(path.read_text(encoding="utf-8"))
        evidence_value["verifier"] = {
            "status": "partial" if value["verifier"]["status"] == "implemented" else "implemented",
            "testId": value["verifier"]["testId"],
        }
        evidence = tmp_path / f"evidence-{path.name}"
        evidence.write_text(json.dumps(evidence_value), encoding="utf-8")
        flipped = ProfileRegistry.from_file(evidence)
        assert flipped.description_digest == registered.description_digest
        assert flipped.description.pin(description_digest=flipped.description_digest) == description.pin(
            description_digest=registered.description_digest
        )

def test_unpinned_descriptions_are_rejected_at_load(tmp_path: Path) -> None:
    for path in _storage_profile_paths():
        value = json.loads(path.read_text(encoding="utf-8"))
        value["configuration"] = {**value["configuration"], "driftedSetting": True}
        unpinned = tmp_path / f"unpinned-{path.name}"
        unpinned.write_text(json.dumps(value), encoding="utf-8")
        with pytest.raises(ProfileError, match="configuration digest differs"):
            ProfileRegistry.from_file(unpinned)


def _seeded_local_run(tmp_path: Path, profiles: ProfileSet) -> tuple[Path, dict[str, str]]:
    source_content = tmp_path / "source-content"
    source_content.mkdir()
    source_bytes = b"One conformance paragraph."
    (source_content / "document.txt").write_bytes(source_bytes)
    source_catalog_root = tmp_path / "source-catalog"
    source_ref = write_shared_source_catalog(
        source_catalog_root,
        (
            SourceItem(
                "document-a",
                "v1",
                (
                    CandidateFile(
                        "primary",
                        "document.txt",
                        "text/plain",
                        expected_digest=sha256_digest(source_bytes),
                        expected_size=len(source_bytes),
                        transport_version="fixture:v1",
                    ),
                ),
                metadata={"expectedSegments": 1},
            ),
        ),
    )
    retry = RetryPolicy()
    accepted = AcceptedFailurePolicy()
    processor = ContentStatisticsProcessor()
    plan = ProcessingPlan.create(
        source_catalog=source_ref,
        base_release=None,
        profiles=profiles,
        limits=WorkLimits(2, 1024 * 1024, 10, 10, 100, 1024 * 1024, 60, retry.max_attempts),
        stages=StagePolicy(
            (DefaultExtractorRegistry.extractor_id,),
            DefaultSegmenterRegistry.segmenter_id,
            (processor.description.processor_id,),
        ),
        processors=ProcessorSet((processor.description,)),
        partition_count=4,
        selection={},
        retention_policy=RetentionPolicy.retain_all(),
        data_use_policy=DataUsePolicy.local_content(),
        retry_policy_digest=retry.digest,
        accepted_failure_policy_digest=accepted.digest,
    )
    plan_path = tmp_path / "plan.json"
    plan_path.write_bytes(canonical_json_file_bytes(plan.to_dict()))
    roots = {
        "blobStorage": (tmp_path / "blobs").as_posix(),
        "controlRepository": (tmp_path / "controls").as_posix(),
        "documentCatalog": (tmp_path / "catalog").as_posix(),
        "documentStores": (tmp_path / "stores").as_posix(),
        "reconciliation": (tmp_path / "reconciliation").as_posix(),
        "recordStorage": (tmp_path / "records").as_posix(),
        "sourceCatalog": source_catalog_root.as_posix(),
        "sourceContent": source_content.as_posix(),
    }
    request = _write_local_run_request(
        tmp_path / "run-request.json",
        plan_path=plan_path,
        roots=roots,
        result_sink_id="urn:docspec:test:sink:local-durable",
        retry=retry,
        accepted=accepted,
        completed_at="2026-08-05T12:00:00Z",
    )
    return request, roots


@pytest.mark.parametrize(
    ("tamper", "expected_message"),
    [
        ("unknown-profile", "unknown selected profile"),
        ("description-digest-drift", "differ from their machine descriptions"),
        ("capability-drift", "differ from their machine descriptions"),
        ("configuration-digest-drift", "differ from their machine descriptions"),
    ],
)
def test_unknown_unpinned_capability_and_digest_mismatched_pins_fail_before_work(
    tmp_path: Path,
    capfd: pytest.CaptureFixture[str],
    tamper: str,
    expected_message: str,
) -> None:
    """A plan pin the machine descriptions do not corroborate must stop
    `run start` before any planning state exists, per the negative-fixture
    families of specification section 15.3."""

    genuine = _portable_local_profiles()
    pins = list(genuine.pins)
    if tamper == "unknown-profile":
        pins[0] = replace(pins[0], profile_id=f"{pins[0].profile_id}-unknown")
    elif tamper == "description-digest-drift":
        pins[0] = replace(pins[0], description_digest=identity_digest({"drift": True}))
    elif tamper == "capability-drift":
        pins[0] = replace(pins[0], capabilities=("undeclared-capability",))
    else:
        pins[0] = replace(pins[0], configuration_digest=identity_digest({"drift": True}))
    request, roots = _seeded_local_run(tmp_path, ProfileSet(tuple(pins)))

    destination = tmp_path / "run-reference.json"
    receipt = tmp_path / "run-operation.json"
    assert (
        main(
            [
                "run",
                "start",
                "--request",
                str(request),
                "--destination",
                str(destination),
                "--receipt",
                str(receipt),
            ]
        )
        == 2
    )
    error = json.loads(capfd.readouterr().err)
    assert error["verdict"] == "fail"
    assert expected_message in error["message"]
    failure = json.loads(receipt.read_text(encoding="utf-8"))
    assert failure["verdict"] == "failed"
    assert failure["operation"] == "run.start"
    assert not destination.exists()
    assert not Path(roots["controlRepository"]).exists(), "profile rejection must precede planning state"
