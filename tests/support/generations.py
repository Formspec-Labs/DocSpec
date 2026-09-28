"""Sealed spicy-regs rollup generations and their publication pointers, written the way the producer does."""

import shutil

import duckdb
import httpx
import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq
from rulespec_artifacts import (ArtifactPin, LocalMemberSource, Producer, build_artifact_root, canonical_json_bytes,
    describe_member, write_member_manifest)

from docspec.adapters.content_fetchers.https import HttpsContentFetcher

# Rulespec requires a producer pinned by a published digest or full Git object ID.
_IMPLEMENTATION = "git+https://example.test/spicy-regs@" + "1" * 40
PRODUCER = Producer("spicy-regs", _IMPLEMENTATION, "urn:test:verifier", "1", _IMPLEMENTATION)
_DEFAULT_ROWS = {
    "federal_register": {"document_number": ["2026-1"], "publication_date": ["2026-09-25"], "title": ["A rule"]},
    "congress_bills": {"bill_id": ["119-hr-1"], "title": ["A bill"]},
    "bill_actions": {"bill_id": ["119-hr-1"], "action_index": ["1"]},
}


def seal_generation(path, family, members, tables, *, status="complete-family", kind="spicy-regs-rollup-generation"):
    """Write the member manifest and root over ``members`` and the ``tables`` descriptors; return the pin."""
    with (path / "members.json").open("wb") as output:
        manifest = write_member_manifest(output, scope_kind="global", scope_id=family, object_key="members.json",
                                         members=members)
    root = build_artifact_root(kind=kind, spec={"family": family, "publicationStatus": status, "tables": tables},
                               producer=PRODUCER, manifests=[manifest])
    (path / "artifact.json").write_bytes(canonical_json_bytes(root))
    return ArtifactPin(root["logicalId"], root["artifactDigest"])


def _described_columns(path):
    """A Parquet file's columns as the producer describes them: DuckDB's footer names, hive partitioning off."""
    with duckdb.connect() as connection:
        return [[name, kind] for name, kind, *_ in connection.execute(
            "DESCRIBE SELECT * FROM read_parquet(?, hive_partitioning = false)", [str(path)]).fetchall()]


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
    description = {"columns": _described_columns(path / member.object_key) if columns is None else columns,
                   "rows": member.record_count}
    if identity is not None:
        description["identity"] = identity
    pin = seal_generation(path, family, [member], {member.object_key: description}, status=status, kind=kind)
    return pin, member, description


def split_members(data, column):
    """``data`` split as spicy-regs' builder lays out a split table: one ``<column>=<value>/part-000000.parquet`` each."""
    return [(f"{column}={value}/part-000000.parquet", data.filter(pc.equal(data[column], value)))
            for value in sorted(set(data[column].to_pylist()))]


def family_generation(path, tables, *, family="bill-family", partitions=None, overrides=None, counts=None,
                      write_statistics=True):
    """Seal one generation of several tables under ``path`` as spicy-regs' ``build_generation(partitioned=…)`` does.

    ``tables`` maps each logical table to a pyarrow table, written as
    ``<table>.parquet``; a table that ``partitions`` names with its partition
    columns maps instead to its members, ``(relative key, pyarrow table)``
    pairs under ``<table>/`` (see ``split_members``). ``overrides`` updates a
    table's descriptor and ``counts`` a member's record count, to make them lie.
    Returns the pin, the member descriptors in key order and each table's
    descriptor by key.
    """
    path.mkdir(parents=True)
    partitions, files = partitions or {}, {}
    for table, data in tables.items():
        for key, part in ([(f"{table}/{relative}", part) for relative, part in data] if table in partitions
                          else [(table + ".parquet", data)]):
            (path / key).parent.mkdir(parents=True, exist_ok=True)
            pq.write_table(part, path / key, write_statistics=write_statistics)
            files[key] = part.num_rows
    source = LocalMemberSource(path)
    members = [describe_member(source, object_key=key, role="table", media_type="application/vnd.apache.parquet",
                               record_count=(counts or {}).get(key, files[key])) for key in sorted(files)]
    descriptions = {}
    for table in tables:
        owned = [member for member in members if member_table(member.object_key) == table + ".parquet"]
        description = {"columns": _described_columns(path / owned[-1].object_key),
                       "rows": sum(member.record_count for member in owned)}
        if table in partitions:
            description["partitionColumns"] = list(partitions[table])
        descriptions[table + ".parquet"] = {**description, **(overrides or {}).get(table, {})}
    return seal_generation(path, family, members, descriptions), members, descriptions


def member_table(key):
    """The table a member key belongs to, as spicy-regs names it: the key, or ``<first directory>.parquet``."""
    return key if "/" not in key else key.split("/", 1)[0] + ".parquet"


def publish(base, source, pin, members, descriptions, *, family="bill-family", prefix=None, versions=(2, 1)):
    """Publish ``source`` under ``base`` as spicy-regs does; return the version-2 and version-1 pointer values.

    Version 2 names every table, a split one by its members; version 1 is
    derived from it and omits split tables, and a family left with none.
    ``versions`` chooses which of ``publication.v2.json`` and ``publication.json`` are written.
    """
    prefix = prefix or f"generations/{family}/current"
    target = base / prefix
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    tables = {}
    for key, description in descriptions.items():
        owned = [member for member in members if member_table(member.object_key) == key]
        if "partitionColumns" not in description:
            [member] = owned
            tables[key] = {**description, "sha256": member.sha256, "byteSize": member.byte_size}
            continue
        tables[key] = {**description, "byteSize": sum(member.byte_size for member in owned), "members": [
            {"key": member.object_key, "sha256": member.sha256, "byteSize": member.byte_size, "rows": member.record_count,
             "partition": dict(part.split("=", 1) for part in member.object_key.split("/")[1:-1])} for member in owned]}
    entry = {"prefix": prefix, "logicalId": pin.logical_id, "artifactDigest": pin.artifact_digest, "tables": tables}
    single = {key: table for key, table in tables.items() if "members" not in table}
    pointers = {2: {"format": "spicy-regs-publication", "version": 2, "families": {family: entry}},
                1: {"format": "spicy-regs-publication", "version": 1,
                    "families": {family: {**entry, "tables": single}} if single else {}}}
    for version, key in ((2, "publication.v2.json"), (1, "publication.json")):
        if version in versions:
            (base / key).write_bytes(canonical_json_bytes(pointers[version]))
    return pointers[2], pointers[1]


def publication(base, source, pin, member, description, *, family="federal-register", prefix=None):
    """Publish ``source`` under ``base`` with only the ``publication.json`` pointer, as before version 2; return it."""
    return publish(base, source, pin, [member], {member.object_key: description}, family=family, prefix=prefix,
                   versions=(1,))[1]


def serve_https(monkeypatch, base, *, status=None):
    """Serve directory ``base`` at ``https://example.test/data`` through the existing HTTPS transport.

    A missing object answers 404, as R2 does; ``status`` maps an object key to
    another status to answer instead, or to a list of answers to give first, in
    order, before serving it: each a status or a (status, headers) pair.
    Returns the requested URLs and the clients made, so a test can check order
    and closing.
    """
    requests, clients, status = [], [], status or {}
    fixed = {key: answer for key, answer in status.items() if not isinstance(answer, list)}
    queued = {key: list(answers) for key, answers in status.items() if isinstance(answers, list)}

    def respond(request):
        requests.append(str(request.url))
        key = request.url.path.removeprefix("/data/")
        answer = queued[key].pop(0) if queued.get(key) else fixed.get(key)
        if answer is None and not (base / key).exists():
            answer = 404
        if answer is not None:
            code, headers = answer if isinstance(answer, tuple) else (answer, {})
            return httpx.Response(code, headers=headers, request=request)
        return httpx.Response(200, stream=httpx.ByteStream((base / key).read_bytes()), request=request)

    def fetcher(config):
        clients.append(httpx.Client(transport=httpx.MockTransport(respond)))
        return HttpsContentFetcher(clients[-1], config)
    monkeypatch.setattr(HttpsContentFetcher, "from_httpx", fetcher)
    return requests, clients
