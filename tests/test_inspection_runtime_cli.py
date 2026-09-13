"""A supported read path works without executing plugins or changing dataset state."""

import json
from dataclasses import replace
from pathlib import Path

import pytest

from docspec.cli import main
from docspec.cli.requests import _local_run_arguments, _local_run_request
from docspec.domain.identity import canonical_json_file_bytes
from docspec.errors import IntegrityError
from docspec.profile_registry import ProfileRegistry
from docspec.runtime import open_local_inspection, prepare_local_run
from tests.helpers import SharedFixtureContentFetcher
from tests.support.profiles import _seeded_local_run


@pytest.fixture
def retained(tmp_path):
    request, _ = _seeded_local_run(tmp_path, ProfileRegistry.builtin().local_profiles())
    arguments = _local_run_arguments(_local_run_request(request))
    arguments["content_fetcher"] = SharedFixtureContentFetcher(arguments["workspace"].roots["sourceContent"])
    with prepare_local_run(**arguments) as prepared:
        run = prepared.run()
        release = prepared.retain(run)
    run_path = tmp_path / "run-ref.json"
    run_path.write_bytes(canonical_json_file_bytes(run.to_dict()))
    release_path = tmp_path / "release-ref.json"
    release_path.write_bytes(canonical_json_file_bytes(release.to_dict()))
    return request, arguments, run, release, run_path, release_path


def _open(arguments, **references):
    return open_local_inspection(
        arguments["plan"], arguments["workspace"],
        document_release_producer=arguments["document_release_producer"], **references,
    )


def _snapshot(root: Path):
    return {str(path.relative_to(root)): (path.stat().st_size, path.stat().st_mtime_ns)
            for path in root.rglob("*")}


def test_public_reader_opens_exact_work_and_result_without_plugins_or_workspace_writes(retained, monkeypatch):
    _, arguments, run, release, _, _ = retained
    def no_execution(*args, **kwargs):
        pytest.fail("inspection constructed execution services")
    monkeypatch.setattr("docspec.runtime._compose_local_run", no_execution)
    before = _snapshot(arguments["workspace"].root)
    work = _open(arguments, run_ref=run)
    result = _open(arguments, release_ref=release)
    assert work.summary()["work"]["counts"]["scheduledItems"] == 1
    assert result.summary()["result"]["layers"]["files"] == 1
    assert result.summary()["unavailable"]["sourceOutcomes"]
    assert work.compare(result)["result"]["changeCount"] == 0
    assert _snapshot(arguments["workspace"].root) == before


def test_source_coverage_requires_its_own_explicit_producer(retained):
    _, arguments, _, release, _, _ = retained
    result = _open(arguments, release_ref=release, source_catalog_producer=arguments["source_catalog_producer"])
    assert result.summary()["source"]["itemCount"] == 1
    wrong = replace(arguments["source_catalog_producer"], implementation_id="unaccepted-source")
    with pytest.raises(IntegrityError):
        _open(arguments, release_ref=release, source_catalog_producer=wrong)


def test_cli_summary_detail_records_and_comparison_share_public_views(retained, capfd):
    request, _, _, _, run_path, release_path = retained
    def command(name, *options):
        capfd.readouterr()
        assert main(["inspect", name, "--request", str(request), *options]) == 0
        return json.loads(capfd.readouterr().out)

    release_args = ("--release-reference", str(release_path))
    summary = command("summary", *release_args, "--source-coverage")
    assert summary["phase"] == "retained-result"
    assert summary["source"]["itemCount"] == 1
    source = summary["work"]["sample"][0]["sourceItemId"]
    detail = command("source", *release_args, "--source-item-id", source, "--sample-limit", "0")
    assert detail["result"]["layers"]["files"] == {"count": 1, "sample": [], "sampleTruncated": True}
    rows = command("records", *release_args, "--layer-kind", "files", "--sample-limit", "0")
    assert rows["records"] == [] and rows["truncated"]
    comparison = command("compare", "--run-reference", str(run_path), "--other-request", str(request),
                         "--other-release-reference", str(release_path))
    assert comparison["result"]["changeCount"] == 0
    capfd.readouterr()
    assert main(["inspect", "summary", "--request", str(request), "--sample-limit", "-1"]) == 2
    assert "sample limit" in capfd.readouterr().err
