"""Create sealed source-catalog policies through their application owners.

Selectors and samples use the closed shapes those owners already parse. An
agency-name mapping may be inline or relative to the input file. Omitted
optional fields retain the policy defaults. Prove the member round-trip before
exclusive creation so an existing member or symlink is never overwritten.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from docspec.application.federal_register_catalog import FederalRegisterCatalogPolicy
from docspec.application.regulations_gov_catalog import (
    RegulationsGovCatalogPolicy,
    RegulationsGovSamplePolicy,
)
from docspec.domain.identity import canonical_json_file_bytes
from docspec.ports.source_catalog import SourceInputSelector
from docspec.cli_io import MAX_JSON_BYTES, SourceCatalogCliError, emit, read_object

_POLICY_CHOICES = ("regulations-gov", "federal-register")


def _optional_selector(value: object) -> SourceInputSelector | None:
    return None if value is None else SourceInputSelector.from_dict(value)


def _agency_names(value: object, *, input_path: Path) -> dict[str, str]:
    if isinstance(value, str):
        mapping_path = Path(value)
        if not mapping_path.is_absolute():
            mapping_path = input_path.parent / mapping_path
        value = read_object(mapping_path, label="agency names", error_type=SourceCatalogCliError)
    if not isinstance(value, dict):
        raise ValueError("agency_names must be a JSON object or a path to one")
    return dict(value)


def build_policy(policy_name: str, fields: dict[str, Any], *, input_path: Path) -> FederalRegisterCatalogPolicy | RegulationsGovCatalogPolicy:
    if policy_name == "regulations-gov":
        if fields.get("document_input") is None:
            raise ValueError("regulations-gov document_input is required and may not be null")
        sample = fields.get("sample")
        return RegulationsGovCatalogPolicy(
            document_input=SourceInputSelector.from_dict(fields["document_input"]),
            docket_input=_optional_selector(fields.get("docket_input")),
            federal_register_input=_optional_selector(fields.get("federal_register_input")),
            agency_names=_agency_names(fields.get("agency_names"), input_path=input_path),
            sample=None if sample is None else RegulationsGovSamplePolicy.from_dict(sample),
            max_selected_items=fields.get("max_selected_items"),
            comment_input=_optional_selector(fields.get("comment_input")),
            **{
                name: fields[name]
                for name in ("language", "source_url_template")
                if name in fields
            },
        )
    if policy_name == "federal-register":
        return FederalRegisterCatalogPolicy(fields.get("expected_source_system_id"))
    raise ValueError(f"unsupported --policy: {policy_name}")


def write_member(policy_name: str, input_path: Path, output_path: Path) -> bytes:
    fields = read_object(
        input_path, label="catalog policy fields", error_type=SourceCatalogCliError
    )
    policy = build_policy(policy_name, fields, input_path=input_path)
    member_bytes = canonical_json_file_bytes(policy.to_member())
    if len(member_bytes) > MAX_JSON_BYTES:
        raise ValueError(f"the policy member exceeds the CLI's {MAX_JSON_BYTES}-byte limit")

    round_tripped = type(policy).from_member(json.loads(member_bytes))
    if canonical_json_file_bytes(round_tripped.to_member()) != member_bytes:
        raise AssertionError("round-trip through from_member produced different bytes")

    # Every proof above is complete before anything is written. "x" is
    # O_CREAT|O_EXCL, which refuses an existing file and a symlink alike, so a
    # sealed policy member is never overwritten in place.
    with output_path.open("xb") as stream:
        stream.write(member_bytes)
    return member_bytes



def write_policy(args: argparse.Namespace) -> int:
    """Write a verified policy member and return its existing canonical shape."""
    member_bytes = write_member(args.policy, args.input, args.output)
    emit(json.loads(member_bytes))
    return 0


def add_policy_command(commands: argparse._SubParsersAction) -> None:
    parser = commands.add_parser("write-policy", help="Create a sealed catalog policy from its configurable fields")
    parser.add_argument("--policy", choices=_POLICY_CHOICES, required=True)
    parser.add_argument("--input", type=Path, required=True, help="JSON file of the policy's configurable fields")
    parser.add_argument("--output", type=Path, required=True, help="New canonical policy member; refuses existing paths")
    parser.set_defaults(func=write_policy)
