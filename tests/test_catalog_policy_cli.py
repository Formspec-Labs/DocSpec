"""Installed-command behavior for creating sealed source-catalog policies."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.application.regulations_gov_catalog import RegulationsGovCatalogPolicy
from docspec.domain.identity import canonical_json_file_bytes
from docspec.entrypoint import main
from docspec.ports.source_catalog import SourceInputSelector


@pytest.mark.parametrize("policy_name,agency_file", [
    ("federal-register", False),
    ("regulations-gov", False),
    ("regulations-gov", True),
])
def test_write_policy_creates_the_application_member(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], policy_name: str, agency_file: bool,
) -> None:
    """The command writes the exact canonical member the policy class serializes and echoes it as JSON."""
    if policy_name == "federal-register":
        expected = FederalRegisterCatalogPolicy("urn:test:federal-register")
        fields = {"expected_source_system_id": "urn:test:federal-register"}
    else:
        selector = SourceInputSelector(
            "urn:test:regulations-gov", "v4", "regulations-gov-documents",
            "regulations-gov-document-raw", "1.0",
        )
        names = {"EPA": "Environmental Protection Agency"}
        expected = RegulationsGovCatalogPolicy(
            document_input=selector, docket_input=None, federal_register_input=None, agency_names=names,
        )
        fields = {"document_input": selector.to_dict(), "agency_names": names}
        if agency_file:
            (tmp_path / "names.json").write_text(json.dumps(names))
            fields["agency_names"] = "names.json"
    source = tmp_path / "fields.json"
    source.write_text(json.dumps(fields))
    output = tmp_path / "policy.json"

    assert main([
        "source-catalog", "write-policy", "--policy", policy_name,
        "--input", str(source), "--output", str(output),
    ]) == 0

    assert output.read_bytes() == canonical_json_file_bytes(expected.to_member())
    assert json.loads(capsys.readouterr().out) == expected.to_member()
    assert type(expected).from_member(json.loads(output.read_bytes())).to_member() == expected.to_member()


@pytest.mark.parametrize("symlink", [False, True])
def test_write_policy_refuses_existing_files_and_symlinks(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], symlink: bool,
) -> None:
    """An existing output file or symlink refuses with exit 2 and leaves the retained bytes untouched."""
    source = tmp_path / "fields.json"
    source.write_text('{"expected_source_system_id":"urn:test:federal-register"}')
    retained = tmp_path / "retained.json"
    retained.write_bytes(b"keep these bytes")
    output = tmp_path / "policy.json"
    if symlink:
        output.symlink_to(retained)
    else:
        output.write_bytes(b"keep these bytes")

    assert main([
        "source-catalog", "write-policy", "--policy", "federal-register",
        "--input", str(source), "--output", str(output),
    ]) == 2

    assert output.read_bytes() == b"keep these bytes"
    assert retained.read_bytes() == b"keep these bytes"
    assert output.is_symlink() == symlink
    assert json.loads(capsys.readouterr().err)["errorType"] == "FileExistsError"


@pytest.mark.parametrize("policy_name,fields", [
    ("regulations-gov", {"document_input": None, "agency_names": {}}),
    ("federal-register", {}),
])
def test_write_policy_refuses_invalid_fields_before_creating_output(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], policy_name: str, fields: dict,
) -> None:
    """Invalid policy fields refuse with exit 2 and leave no output file behind."""
    source = tmp_path / "fields.json"
    source.write_text(json.dumps(fields))
    output = tmp_path / "policy.json"

    assert main([
        "source-catalog", "write-policy", "--policy", policy_name,
        "--input", str(source), "--output", str(output),
    ]) == 2

    assert not output.exists()
    assert json.loads(capsys.readouterr().err)["errorType"] == "ValueError"


@pytest.mark.parametrize("template", [None, 42, {}, ["{documentId}"], "https://example.test/static"])
def test_write_policy_refuses_invalid_url_templates_as_structured_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], template: object,
) -> None:
    """Each invalid template refuses with the structured CLI error and creates no output."""
    selector = SourceInputSelector(
        "urn:test:regulations-gov", "v4", "regulations-gov-documents",
        "regulations-gov-document-raw", "1.0",
    )
    source = tmp_path / "fields.json"
    source.write_text(json.dumps({
        "document_input": selector.to_dict(), "agency_names": {}, "source_url_template": template,
    }))
    output = tmp_path / "policy.json"

    assert main([
        "source-catalog", "write-policy", "--policy", "regulations-gov",
        "--input", str(source), "--output", str(output),
    ]) == 2

    captured = capsys.readouterr()
    assert captured.out == ""
    assert json.loads(captured.err) == {
        "format": "docspec-cli-error", "formatVersion": "1.0", "errorType": "ValueError",
        "message": "Regulations.gov source URL template must contain one {documentId}", "verdict": "fail",
    }
    assert not output.exists()
