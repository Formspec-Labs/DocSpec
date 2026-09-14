"""Smoke the production capacity recipe against independent fixture answers."""

import pytest

from tests.support.core_runtime_experiment import run_stage


def test_production_recipe_covers_selection_revision_reuse_and_checkpoint(tmp_path):
    assert run_stage(tmp_path, "generate", count=16)["rows"] == 16
    built = run_stage(tmp_path, "build")
    assert built["members"] == 16
    assert built["metrics"]["python_fsync_calls"] > 0 and built["metrics"]["python_fsync_seconds"] > 0
    opened = run_stage(tmp_path, "open")
    assert opened["current"] == ("state", "root") and opened["metrics"]["extraction"] == {}
    for stage in ("fields", "named", "whole", "ordered-fields"):
        result = run_stage(tmp_path, stage)
        assert result["rows"] == 16 + (stage == "named")
        assert result["metrics"]["extraction"]["converted_rows"] == result["rows"]
        assert result["retained_position_order_checked"] == (stage == "ordered-fields")
        if stage == "named":
            # Profile all actual parent reads and all inline/external/opaque
            # query passes, including queries returning no external rows.
            assert len(result["query_profiles"]) >= 3
            assert any(profile["scans"] for profile in result["query_profiles"])
            assert result["parent_payload_scan_bound"]["within_64_mib"]
            assert len(result["parent_payload_scan_bound"]["scans"]) == 1
    assert run_stage(tmp_path, "recover")["direct_and_parent_evidence_equal"]
    membership = run_stage(tmp_path, "membership", edit_count=2)
    assert membership["removed_members"] == membership["restored_members"] == 2
    assert membership["complete_addresses_checked"] and membership["restoration_reuses_original_result"]
    edits = run_stage(tmp_path, "edits", edit_count=2)
    assert edits["exact_title_reuse"] and edits["url_result_changed"]
    ranges = edits["query_profile_ranges"]
    assert ranges["baseline"][1] == ranges["title"][0] < ranges["title"][1] == ranges["url"][0] < ranges["url"][1]
    for phase in ("title", "url"):
        start, end = ranges[phase]
        names = {profile["path"] for profile in edits["query_profiles"][start:end]}
        bound = edits["parent_payload_scan_bound"][phase]
        if bound["scans"]:
            assert bound["within_64_mib"]
        assert all(scan["profile"] in names for scan in bound["scans"])
    history = run_stage(tmp_path, "history", history_length=19)
    assert history["state_id"] == "history:18"
    assert run_stage(tmp_path, "audit", state=history["state_id"])["members"] == 16
    run_stage(tmp_path, "clean", state=history["state_id"])
    assert run_stage(tmp_path, "compare", state=history["state_id"])["counts"]["changed"] == 0
    checkpoint = run_stage(tmp_path, "checkpoint", state=history["state_id"])
    assert checkpoint["logical_identity_preserved"] and checkpoint["audit"]["members"] == 16
    assert run_stage(tmp_path, "compare", state=history["state_id"])["counts"]["changed"] == 0
    assert run_stage(tmp_path, "audit")["members"] == 16


@pytest.mark.parametrize("width", (2, 192))
def test_named_fields_open_only_matching_payload_shards(tmp_path, width):
    from docspec.domain import core
    from docspec.runtime import CoreWorkspace
    from tests.support.core_runtime_experiment import QueryProfiles, observations, payload_scan_bound

    with CoreWorkspace(tmp_path / "workspace") as workspace:
        workspace.records.max_member_bytes = 64 * 1024
        workspace.create("root", rows=((f"{index:04d}", {"url": f"url:{index}", "body": "x" * 1024}) for index in range(256)))
        profiles, metrics = QueryProfiles(tmp_path / "profiles"), {}
        definition = core.StateMembers(member_selector=core.JsonFields(selectors=(core.Field(label="url", pointer="/url"),)),
                                       scope=tuple(f"{index:04d}" for index in range(width)) + ("absent",), material_keys=True)
        with observations(workspace, metrics, profiles), workspace.publisher.session() as session:
            selected = workspace.selections.retain(session, selected_value_id="named", definition=definition,
                                                  origin=core.Origin(parent_entity_id="root"))
            rows = list(workspace.selections.rows(session, selected))
        expected = {f"{index:04d}": False for index in range(width)} | {"absent": True}
        assert {key: entity is None for key, entity, _ in rows} == expected
        evidence = payload_scan_bound(workspace, "root", profiles.profiles)
        assert evidence["within_64_mib"]
        assert len(evidence["scans"]) == 1
        # A wide selection may legitimately touch every packed file.
        assert all(scan["files"] <= evidence["parent_files"] for scan in evidence["scans"])
        if width == 2:
            # The two identities may route to separate hash buckets. Each
            # belongs to one payload file; absent keys add no payload reads.
            assert all(1 <= scan["files"] <= width for scan in evidence["scans"])
        else:
            assert all(scan["files"] > 5 and scan["truncated_names_resolved_from_query"] for scan in evidence["scans"])


def test_full_oracle_rejects_duplicate_for_missing_member(tmp_path, monkeypatch):
    from contextlib import closing

    import pytest

    from docspec.runtime import CoreWorkspace
    from tests.support.core_runtime_experiment import audit

    run_stage(tmp_path, "generate", count=4)
    run_stage(tmp_path, "build")
    with CoreWorkspace(tmp_path / "workspace") as workspace:
        original_rows = workspace.rows

        def duplicate(state):
            with closing(original_rows(state)) as rows:
                first = next(rows)
                yield first
                next(rows)
                yield first
                yield from rows

        monkeypatch.setattr(workspace, "rows", duplicate)
        with pytest.raises(AssertionError):
            audit(workspace, "root", 4, 0)
