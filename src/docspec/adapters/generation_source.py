"""Stage and admit a complete producer generation before publishing a table state.

A publication base serves spicy-regs' untrusted pointer: ``publication.v2.json``
when the publisher writes one, else ``publication.json``. Version 2 may list a
table split into several members; version 1 never does (spicy-regs
``docs/research/multi-file-tables-2026-09-26.md``; this repository's
``docs/history/2026-09-27-publication-v2/``).
"""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from typing import NamedTuple
from urllib.parse import quote, urlsplit

import duckdb
import pyarrow.parquet as pq
from rulespec_artifacts import (ArtifactPin, ArtifactVerificationError, LocalMemberSource,
    MemberDescriptor, MemberNotFoundError, MemberSourceError, admit_artifact, iter_member_descriptors,
    parse_canonical_json, validate_object_key)
from spicy_docs.schemas import TABLE_CONTRACTS
from spicy_docs.schemas.tables import KEY_SPELLINGS

from docspec.adapters.content_fetchers.https import HttpsContentFetcher, HttpsContentFetcherConfig, HttpsNotFoundError
from docspec.adapters.storage.iceberg import identifier
from docspec.adapters.storage.records import native_columns
from docspec.adapters.storage.table_sql import declared_spelling
from docspec.domain.content import CandidateFile
from docspec.domain.table_rows import KeySpelling
from docspec.errors import IntegrityError, LimitExceededError


_DOCUMENT_BYTES = 8 * 1024**2
_STAGED_BYTES = 64 * 1024**3
_CHUNK_BYTES = 1024**2
# Read in this order; only absence of the first falls back to the second.
_POINTERS = (("publication.v2.json", 2), ("publication.json", 1))
_PARQUET = "application/vnd.apache.parquet"
_PART = re.compile(r"part-\d{6}\.parquet\Z")


class StagedMember(NamedTuple):
    """One staged Parquet file of the selected table and its Rulespec member descriptor."""

    path: Path
    descriptor: MemberDescriptor


@dataclass(frozen=True)
class AdmittedGeneration:
    """The selected table and its exact source evidence, valid inside the staging context.

    ``members`` are the table's staged files in object-key order: one for a
    single-file table, or several for a table published split, whose
    ``partition_columns`` are then nonempty. ``columns`` names each column with
    its table-profile type, ready for a TableSchema; ``key`` is the declared
    member-key spelling.
    """
    pin: ArtifactPin
    root_bytes: bytes
    manifest_bytes: bytes
    members: tuple[StagedMember, ...]
    partition_columns: tuple[str, ...]
    columns: tuple[tuple[str, str], ...]
    record_count: int
    key: KeySpelling


def _mapping(value, label):
    if not isinstance(value, dict):
        raise IntegrityError(f"{label} must be an object")
    return value


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise IntegrityError("generation pointer repeats an object key")
        result[key] = value
    return result


def _pointer(payload, version):
    try:
        value = json.loads(payload, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as error:
        raise IntegrityError("generation pointer is not valid JSON") from error
    value = _mapping(value, "publication")
    if value.get("format") != "spicy-regs-publication" or type(value.get("version")) is not int or value["version"] != version:
        raise IntegrityError("unsupported generation publication pointer")
    return value


def _publication(chunks):
    """The version-2 pointer when the publisher serves one, else version 1.

    Only absence (a missing local file, an HTTPS 404) falls back; any other
    failure refuses. Each key must hold its own version.
    """
    for key, version in _POINTERS:
        try:
            payload = _read(chunks, key)
        except (MemberNotFoundError, HttpsNotFoundError):
            if version == 1:
                raise
            continue
        return _pointer(payload, version)


# Decision 0003's spelling of a Federal Register record, the fallback while the
# installed spicy-docs has no federal_register contract to declare it.
_FEDERAL_REGISTER = ("federal-register", "federal_register")
_FEDERAL_REGISTER_KEY = ("document_number", "publication_date"), "federal-register-source-record-id/1"


def _key_rule(family, table, description, columns):
    """The member-key spelling a table is admitted under, as its producer declares it.

    Identity fields the artifact declares come first; one field spells as
    ``value/1``. Otherwise the table's spicy-docs contract names the identity
    and its ``name/version`` key spelling, an entry of ``KEY_SPELLINGS`` whose
    function is the Python reference DocSpec's SQL is checked against; a
    contract that declares none refuses, a composite (ruling R6) or a single
    column. Decision 0003's ``number@date`` applies to Federal Register only
    when spicy-docs has no contract for it, and is the spelling that contract
    declares. ``columns`` are the member's, with table-profile types.
    """
    contract = TABLE_CONTRACTS.get(table)
    artifact = fields = description.get("identity")
    name = None
    if contract is not None and (fields is None or fields == list(contract.identity)):
        fields, name = list(contract.identity), getattr(contract, "key_spelling", None)
    elif fields is None and (family, table) == _FEDERAL_REGISTER:
        fields, name = list(_FEDERAL_REGISTER_KEY[0]), _FEDERAL_REGISTER_KEY[1]
    if not isinstance(fields, (list, tuple)) or not fields or any(not isinstance(field, str) or not field for field in fields):
        raise IntegrityError("table has no declared identity fields")
    kinds = dict(columns)
    if len(set(fields)) != len(fields) or not set(fields) <= kinds.keys():
        raise IntegrityError("table identity fields differ from its columns")
    if name is None and artifact is not None and len(fields) == 1:
        name = "value/1"  # an artifact's own one-field identity is spelled by its value
    if name is None:
        # A provisional spelling would silently change identity when spicy-docs declares one.
        raise IntegrityError("composite table identity requires a versioned spicy-docs key spelling" if len(fields) > 1
                             else "table contract declares no key spelling for its identity")
    spelling_id, _, version = name.rpartition("/")
    if not spelling_id or not version:
        raise IntegrityError(f"key spelling {name!r} is not a name/version")
    if name not in KEY_SPELLINGS and (fields, name) != (list(_FEDERAL_REGISTER_KEY[0]), _FEDERAL_REGISTER_KEY[1]):
        raise IntegrityError(f"spicy-docs declares no key spelling {name}")
    spelling = KeySpelling(spelling_id, version, tuple(fields))
    declared_spelling(spelling, tuple(kinds[field] for field in fields))
    return spelling


def _member_table(key):
    """The table a member key belongs to: the key itself for a single file, else ``<first directory>.parquet``."""
    head, separator, _ = key.partition("/")
    return head + ".parquet" if separator else key


def _partition(table, key, columns):
    """The partition a split member's key spells, refusing any key but ``<table>/<col>=<value>/…/part-NNNNNN.parquet``.

    The directories name ``columns`` in order, each with a nonempty value.
    """
    parts = key.split("/")
    directories = [part.partition("=") for part in parts[1:-1]]
    if (_member_table(key) != table or not _PART.fullmatch(parts[-1])
            or [column for column, _, _ in directories] != list(columns)
            or any(separator != "=" or not value for _, separator, value in directories)):
        raise IntegrityError(f"split member {key!r} does not spell its partition as <table>/<col>=<value>/part-NNNNNN.parquet")
    return {column: value for column, _, value in directories}


def _check_partition(connection, path, partition):
    """Refuse a split member whose rows hold other values than its key names, compared as text.

    Every row group's footer statistics showing no nulls and min = max = the
    value prove it without reading data; a member without such statistics has
    that one column scanned instead. Statistics only ever prove agreement.
    """
    for column, value in partition.items():
        groups = connection.execute("SELECT stats_min_value, stats_max_value, stats_null_count FROM parquet_metadata(?) "
                                    "WHERE path_in_schema = ?", [str(path), column]).fetchall()
        if groups and all(low == high == value and nulls == 0 for low, high, nulls in groups):
            continue
        # Hive partitioning off: the member's <col>=<value> directory must not stand in for its column.
        differing = connection.execute(f"SELECT count(*) FROM read_parquet(?, hive_partitioning = false) "
                                       f"WHERE CAST({identifier(column)} AS VARCHAR) IS DISTINCT FROM ?",
                                       [str(path), value]).fetchone()[0]
        if differing:
            raise IntegrityError(f"split member rows differ from the partition {column}={value} its key names")


def _check_table(connection, staging, name, description, members, indexed):
    """Check one table's members against its descriptor and publication entry; return its columns and partitioning.

    A table without ``partitionColumns`` is exactly one member at its own key.
    A split table's partition columns are distinct declared columns; each
    member spells its partition in its key and holds only that partition's
    rows, member digests are distinct, and member record counts sum to the
    table's rows. Every member's footer rows equal its record count and its
    columns, read with hive partitioning off, equal the declared ones, so
    mixed schemas refuse. A publication entry must equal the artifact's, its
    members compared in key order. Only footers and statistics are read.
    """
    declared, split = description.get("columns"), "partitionColumns" in description
    partitioning = description["partitionColumns"] if split else []
    if split and (not isinstance(partitioning, list) or not partitioning or len(set(partitioning)) != len(partitioning)
                  or not isinstance(declared, list)
                  or not set(partitioning) <= {column[0] for column in declared if isinstance(column, list) and column}):
        raise IntegrityError(f"{name} partition columns must be distinct declared columns")
    if not split and [member.object_key for member in members] != [name]:
        raise IntegrityError("generation table set differs from its members or publication index")
    if len({member.sha256 for member in members}) != len(members):
        raise IntegrityError(f"{name} lists the same member bytes twice")
    columns, partitions = None, []
    for member in members:
        if member.role != "table" or member.media_type != _PARQUET or type(member.record_count) is not int:
            raise IntegrityError("generation table descriptor differs from its member")
        path = staging.joinpath(*member.object_key.split("/"))
        with pq.ParquetFile(path) as parquet:
            if parquet.metadata.num_rows != member.record_count:
                raise IntegrityError("Parquet footer row count differs from its descriptor")
        footer = connection.read_parquet(str(path), hive_partitioning=False)
        if [[column, str(kind)] for column, kind in zip(footer.columns, footer.types, strict=True)] != declared:
            raise IntegrityError("Parquet footer schema differs from its descriptor")
        columns = native_columns(footer)
        if split:
            partitions.append(_partition(name, member.object_key, partitioning))
            _check_partition(connection, path, partitions[-1])
    if type(description.get("rows")) is not int or sum(member.record_count for member in members) != description["rows"]:
        raise IntegrityError("generation table descriptor differs from its member")
    if indexed is not None:
        entry = _mapping(indexed[name], "publication table")
        if split:
            expected = {**description, "byteSize": sum(member.byte_size for member in members), "members": [
                {"key": member.object_key, "sha256": member.sha256, "byteSize": member.byte_size,
                 "rows": member.record_count, "partition": partition} for member, partition in zip(members, partitions)]}
            listed = entry.get("members")
            entry = {**entry, "members": sorted(listed, key=lambda item: item["key"]) if isinstance(listed, list) else listed}
        else:
            expected = {**description, "byteSize": members[0].byte_size, "sha256": members[0].sha256}
        if entry != expected:
            raise IntegrityError("publication table descriptor differs from its pinned member")
    return columns, tuple(partitioning)


def _contract_types(table, columns):
    """Refuse a member whose footer types differ from its table's typed spicy-docs contract.

    Only a contract that types a column is checked, since an untyped contract's
    tables may be published natively typed. Each column both name must have the
    contract's type (VARCHAR unless ``types`` names it). ``columns`` are the
    member's, spelled as table profiles spell them, as ``COLUMN_TYPES`` are.
    A column only one side names is not this check's concern.
    """
    contract = TABLE_CONTRACTS.get(table)
    if contract is None or not getattr(contract, "types", None):
        return
    differing = [f"{name} is {kind}, not {contract.column_type(name)}" for name, kind in columns
                 if name in contract.columns and kind != contract.column_type(name)]
    if differing:
        raise IntegrityError(f"Parquet footer types differ from the {table} contract: " + "; ".join(differing))


@contextmanager
def _reader(source):
    """Use the existing safe local reader or host-bounded HTTPS transport."""
    parsed = urlsplit(str(source))
    if parsed.scheme:
        if parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment:
            raise IntegrityError("generation source must be a local directory or HTTPS base URL")
        config = HttpsContentFetcherConfig((parsed.hostname,), "DocSpec generation admission")
        fetcher = HttpsContentFetcher.from_httpx(config)
        try:
            def chunks(key, limit):
                key = validate_object_key(key, path="generation object")
                url = str(source).rstrip("/") + "/" + "/".join(quote(part, safe="") for part in key.split("/"))
                candidate = CandidateFile(key, url, "application/octet-stream")
                with fetcher.fetch(candidate, max_bytes=limit, task_id="generation-admission", attempt_id="stage") as stream:
                    yield from stream.chunks
            yield chunks, True
        finally:
            fetcher.client.close()
    else:
        local = LocalMemberSource(Path(source))
        def chunks(key, limit):
            with local.open(key) as stream:
                seen = 0
                while chunk := stream.read(_CHUNK_BYTES):
                    seen += len(chunk)
                    if seen > limit:
                        raise LimitExceededError("generation member exceeds its staging byte limit")
                    yield chunk
        yield chunks, False


def _read(chunks, key):
    return b"".join(chunks(key, _DOCUMENT_BYTES))


def _stage(chunks, prefix, staging, root_bytes, max_bytes):
    """Copy declared members only; shared admission owns their hash verification."""
    root = _mapping(parse_canonical_json(root_bytes), "generation root")
    manifests = root.get("memberManifests")
    if not isinstance(manifests, list) or len(manifests) != 1 or manifests[0].get("objectKey") != "members.json":
        raise IntegrityError("generation requires one members.json manifest")
    manifest_bytes = _read(chunks, prefix + "members.json")
    manifest = _mapping(parse_canonical_json(manifest_bytes), "generation manifest")
    members = manifest.get("members")
    if not isinstance(members, list):
        raise IntegrityError("generation manifest requires members")
    (staging / "artifact.json").write_bytes(root_bytes)
    (staging / "members.json").write_bytes(manifest_bytes)
    total = len(root_bytes) + len(manifest_bytes)
    seen = {"artifact.json", "members.json"}
    for raw in members:
        member = MemberDescriptor.from_dict(raw, path="generation/member")
        if member.object_key is None or member.object_key in seen:
            raise IntegrityError("generation requires distinct local member objects")
        seen.add(member.object_key)
        total += member.byte_size
        if total > max_bytes:
            raise LimitExceededError("generation exceeds its aggregate staging byte limit")
        path = staging.joinpath(*member.object_key.split("/"))
        path.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        with path.open("xb") as output:
            for chunk in chunks(prefix + member.object_key, member.byte_size or 1):
                size += len(chunk)
                output.write(chunk)
        if size != member.byte_size:
            raise IntegrityError("generation member size differs from its descriptor")
    return manifest_bytes


@contextmanager
def stage_generation(source, *, family, table, directory=None, expected_pin=None, max_bytes=_STAGED_BYTES):
    """Admit one complete family, yielding its selected table in a temporary directory.

    A local generation directory supplies its self-declared pin unless the caller
    supplies expected_pin. A publication base supplies an untrusted pinned
    pointer, version 2 when served (``_publication``); a version-1 pointer lists
    no split table. Every family member is staged and checked by Rulespec
    admission and ``_check_table``; only the selected table is returned, as one
    member or, published split, several. Producer files are never moved or
    modified.
    """
    if not isinstance(family, str) or not family or not isinstance(table, str) or not table or "/" in table or "\\" in table:
        raise ValueError("family and logical table name must be nonempty strings")
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    filename = validate_object_key(table + ".parquet", path="generation table")
    try:
        with ExitStack() as stack:
            chunks, remote = stack.enter_context(_reader(source))
            staging = Path(stack.enter_context(TemporaryDirectory(prefix="generation-", dir=directory)))
            indexed, prefix = None, ""
            # Direct local generations need no mutable publication pointer.
            direct = not remote and (Path(source) / "artifact.json").exists()
            if not direct:
                publication = _publication(chunks)
                selected = _mapping(_mapping(publication.get("families"), "publication families").get(family), "publication family")
                pin = ArtifactPin(selected["logicalId"], selected["artifactDigest"])
                prefix = validate_object_key(selected["prefix"], path="generation prefix") + "/"
                indexed = _mapping(selected.get("tables"), "publication tables")
                if publication["version"] == 1 and any("members" in _mapping(entry, "publication table")
                                                       for entry in indexed.values()):
                    raise IntegrityError("a version-1 publication pointer lists a split table")
                if expected_pin is not None and pin != expected_pin:
                    raise IntegrityError("publication differs from the expected generation pin")
            root_bytes = _read(chunks, prefix + "artifact.json")
            root = _mapping(parse_canonical_json(root_bytes), "generation root")
            if direct:
                pin = expected_pin or ArtifactPin(root["logicalId"], root["artifactDigest"])
            manifest_bytes = _stage(chunks, prefix, staging, root_bytes, max_bytes)
            result = {}
            def verify(artifact, member_source):
                spec = _mapping(artifact.root.get("spec"), "generation spec")
                if artifact.root["kind"] != "spicy-regs-rollup-generation" or spec.get("family") != family or spec.get("publicationStatus") != "complete-family":
                    raise IntegrityError("admission requires the selected complete-family rollup generation")
                tables = _mapping(spec.get("tables"), "generation tables")
                grouped = {}
                for member in iter_member_descriptors(artifact, member_source):
                    grouped.setdefault(_member_table(member.object_key), []).append(member)
                if set(tables) != set(grouped) or filename not in grouped or indexed is not None and set(indexed) != set(tables):
                    raise IntegrityError("generation table set differs from its members or publication index")
                with duckdb.connect() as connection:
                    for name, members in grouped.items():
                        members.sort(key=lambda member: member.object_key)
                        description = _mapping(tables[name], "generation table")
                        canonical, partitioning = _check_table(connection, staging, name, description, members, indexed)
                        if name == filename:
                            _contract_types(table, canonical)
                            result.update(members=tuple(StagedMember(staging.joinpath(*member.object_key.split("/")), member)
                                                        for member in members),
                                          partition_columns=partitioning, columns=canonical,
                                          record_count=description["rows"], key=_key_rule(family, table, description, canonical))
            artifact = admit_artifact(LocalMemberSource(staging), expected_pin=pin, root_byte_limit=_DOCUMENT_BYTES,
                                      manifest_byte_limit=_DOCUMENT_BYTES, semantic_verifier=verify)
            yield AdmittedGeneration(pin=artifact.pin, root_bytes=root_bytes, manifest_bytes=manifest_bytes, **result)
    except (ArtifactVerificationError, MemberSourceError, KeyError, TypeError, ValueError, OSError, duckdb.Error) as error:
        raise IntegrityError(f"generation admission refused: {error}") from error
