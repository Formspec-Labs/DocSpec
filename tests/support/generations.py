"""Sealed spicy-regs rollup generations and their publication pointers, written the way the producer does."""

import shutil

import duckdb
import pyarrow as pa
import pyarrow.parquet as pq
from rulespec_artifacts import (ArtifactPin, LocalMemberSource, Producer, build_artifact_root, canonical_json_bytes,
    describe_member, write_member_manifest)

# Rulespec requires a producer pinned by a published digest or full Git object ID.
_IMPLEMENTATION = "git+https://example.test/spicy-regs@" + "1" * 40
PRODUCER = Producer("spicy-regs", _IMPLEMENTATION, "urn:test:verifier", "1", _IMPLEMENTATION)
_DEFAULT_ROWS = {
    "federal_register": {"document_number": ["2026-1"], "publication_date": ["2026-09-25"], "title": ["A rule"]},
    "congress_bills": {"bill_id": ["119-hr-1"], "title": ["A bill"]},
    "bill_actions": {"bill_id": ["119-hr-1"], "action_index": ["1"]},
}


def generation(path, data=None, *, table="federal_register", family="federal-register", status="complete-family",
               kind="spicy-regs-rollup-generation", columns=None, rows=None, identity=None):
    """Seal one generation of ``data`` (a pyarrow table) under ``path``; return its pin, member and descriptor.

    The descriptor names the columns as DuckDB reads the footer, unless
    ``columns`` or ``rows`` override them to make it lie.
    """
    path.mkdir(parents=True)
    data = pa.table(_DEFAULT_ROWS[table]) if data is None else data
    pq.write_table(data, path / (table + ".parquet"))
    member = describe_member(LocalMemberSource(path), object_key=table + ".parquet", role="table",
                             media_type="application/vnd.apache.parquet", record_count=data.num_rows if rows is None else rows)
    with (path / "members.json").open("wb") as output:
        manifest = write_member_manifest(output, scope_kind="global", scope_id=family, object_key="members.json", members=[member])
    if columns is None:
        with duckdb.connect() as connection:
            columns = [[name, kind] for name, kind, *_ in connection.execute(
                "DESCRIBE SELECT * FROM read_parquet(?)", [str(path / (table + ".parquet"))]).fetchall()]
    description = {"columns": columns, "rows": member.record_count}
    if identity is not None:
        description["identity"] = identity
    root = build_artifact_root(kind=kind, spec={"family": family, "publicationStatus": status, "tables": {member.object_key: description}},
                               producer=PRODUCER, manifests=[manifest])
    (path / "artifact.json").write_bytes(canonical_json_bytes(root))
    return ArtifactPin(root["logicalId"], root["artifactDigest"]), member, description


def publication(base, source, pin, member, description, *, family="federal-register", prefix=None):
    """Publish ``source`` under ``base`` with a ``publication.json`` pointer naming its pin and table."""
    prefix = prefix or f"generations/{family}/current"
    target = base / prefix
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    value = {"format": "spicy-regs-publication", "version": 1, "families": {family: {
        "prefix": prefix, "logicalId": pin.logical_id, "artifactDigest": pin.artifact_digest,
        "tables": {member.object_key: {**description, "sha256": member.sha256, "byteSize": member.byte_size}}}}}
    (base / "publication.json").write_bytes(canonical_json_bytes(value))
    return value
