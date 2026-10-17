"""Stage and admit a complete producer generation before publishing a table state.

A publication base serves spicy-regs' untrusted ``publication.v2.json`` pointer.
Version 2 supports single-file and split tables (spicy-regs
``docs/research/multi-file-tables-2026-09-26.md``; this repository's
``docs/history/2026-09-27-publication-v2/``).
"""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
import re
from tempfile import TemporaryDirectory
from time import sleep
from typing import NamedTuple
from urllib.parse import quote, urlsplit

import duckdb
from rulespec_artifacts import (ArtifactPin, ArtifactVerificationError, LocalMemberSource,
    MemberDescriptor, MemberNotFoundError, MemberSourceError, admit_artifact, iter_member_descriptors,
    parse_canonical_json, validate_object_key)
from spicy_docs.schemas import TABLE_CONTRACTS
from spicy_docs.schemas.tables import KEY_SPELLINGS

from docspec.adapters.content_fetchers.https import (HttpsContentFetcher, HttpsContentFetcherConfig, HttpsNotFoundError,
    HttpsRetryableResponseError)
from docspec.adapters.storage.iceberg import identifier
from docspec.adapters.storage.records import parquet_footers, profile_columns
from docspec.adapters.storage.table_sql import declared_spelling
from docspec.domain.content import CandidateFile
from docspec.domain.table_rows import KeySpelling
from docspec.errors import IntegrityError, LimitExceededError


_DOCUMENT_BYTES = 8 * 1024**2
_STAGED_BYTES = 64 * 1024**3
_CHUNK_BYTES = 1024**2
_PARQUET = "application/vnd.apache.parquet"
_POINTER = "publication.v2.json"
_PART = re.compile(r"part-[0-9]{6}\.parquet\Z")  # ASCII digits only: \d also matches other scripts' digits
# A public read retries the transient answers r2.dev gives under load: in all
# _RETRY_ATTEMPTS requests, waiting the answer's Retry-After seconds or else
# _RETRY_BACKOFF_SECONDS, doubling. A stated wait over _RETRY_MAX_WAIT_SECONDS
# refuses rather than stalls. Any other status keeps its meaning: a 404 is
# absence; no older publication is discovered.
_TRANSIENT_STATUSES = frozenset({429, 502, 503, 504})
_RETRY_ATTEMPTS = 4
_RETRY_BACKOFF_SECONDS = 1.0
_RETRY_MAX_WAIT_SECONDS = 60.0


class StagedMember(NamedTuple):
    """One staged Parquet file of the selected table and its Rulespec member descriptor."""

    path: Path
    descriptor: MemberDescriptor


@dataclass(frozen=True)
class AdmittedGeneration:
    """Selected table metadata and evidence; payload files are valid only after staging.

    ``GenerationSource.selected`` also builds a metadata-only candidate for
    retained lookup. Its relative paths must never be read as staged files.

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
    receipts: tuple[StagedMember, ...] = ()


@dataclass(frozen=True)
class GenerationSource:
    """Bounded, untrusted metadata and its open source reader; no payload staged yet."""
    chunks: object
    prefix: str
    direct: bool
    pin: ArtifactPin
    root_bytes: bytes
    manifest_bytes: bytes
    indexed: dict | None
    indexed_receipts: dict | None

    def selected(self, family, table):
        """Describe a candidate for retained lookup; only exact retained evidence can authorize reuse."""
        from docspec.adapters.generation_receipts import KEY, receipt_index
        root = parse_canonical_json(self.root_bytes)
        manifest = parse_canonical_json(self.manifest_bytes)
        name = table + ".parquet"
        spec = _mapping(root["spec"], "generation spec")
        tables = _mapping(spec["tables"], "generation tables")
        description = _mapping(tables[name], "generation table")
        members = [MemberDescriptor.from_dict(raw, path="generation/member") for raw in manifest["members"]]
        seen = {"artifact.json", "members.json"}
        for member in members:
            if member.object_key is None or member.object_key in seen:
                raise IntegrityError("generation requires distinct local member objects")
            seen.add(member.object_key)
        grouped = {}
        for member in sorted(members, key=lambda member: member.object_key):
            grouped.setdefault(_member_table(member.object_key), []).append(member)
        declaration = spec.get("etlReceipts")
        if declaration is not None:
            declaration = _mapping(declaration, "ETL receipt declaration")
        receipts = grouped.pop(KEY, []) if declaration is not None else []
        if declaration is not None and (len(receipts) != 1 or receipts[0].object_key != KEY):
            raise IntegrityError("Generation must name one shared ETL receipt member")
        if set(tables) != set(grouped):
            raise IntegrityError("generation table set differs from its members")
        selected = grouped[name]
        partitioning = tuple(description.get("partitionColumns", ()))
        if self.indexed is not None:
            if set(self.indexed) != set(tables):
                raise IntegrityError("generation table set differs from its publication index")
            for filename, declared in tables.items():
                declared = _mapping(declared, "generation table")
                _check_index(filename, declared, grouped[filename],
                             self.indexed, declared.get("partitionColumns", ()))
        if not self.direct:
            expected = None if declaration is None else receipt_index(declaration, receipts[0])
            if self.indexed_receipts != expected:
                raise IntegrityError("Publication receipt selection differs from the pinned declaration")
        columns = profile_columns(description["columns"])
        policy = next((policy for policy in (declaration or {}).get("policies", ()) if policy["dataset"] == table), None)
        return AdmittedGeneration(self.pin, self.root_bytes, self.manifest_bytes,
            tuple(StagedMember(Path(m.object_key), m) for m in selected), partitioning, columns, description["rows"],
            _key_rule(family, table, description, columns, policy),
            tuple(StagedMember(Path(m.object_key), m) for m in receipts))


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


def _publication(chunks):
    """Read the one supported pointer, ``publication.v2.json`` version 2; absence or another version refuses.

    No older pointer is discovered: a publisher serving only ``publication.json`` is unsupported.
    """
    try:
        payload = _read(chunks, _POINTER)
    except (MemberNotFoundError, HttpsNotFoundError) as error:
        raise IntegrityError(
            f"generation publication requires {_POINTER}; older publication pointers are unsupported") from error
    try:
        value = json.loads(payload, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as error:
        raise IntegrityError("generation pointer is not valid JSON") from error
    value = _mapping(value, "publication")
    if value.get("format") != "spicy-regs-publication" or type(value.get("version")) is not int or value["version"] != 2:
        raise IntegrityError("unsupported generation publication pointer")
    return value


def _key_rule(family, table, description, columns, policy=None):
    """The member-key spelling a table is admitted under, as its producer declares it.

    Identity fields the artifact declares come first; one field spells as
    ``value/1``. Otherwise the table's spicy-docs contract names the identity
    and its ``name/version`` key spelling, an entry of ``KEY_SPELLINGS`` whose
    function is the Python reference DocSpec's SQL is checked against; a
    contract that declares none refuses. Composite keys (ruling R6) require
    a declared spelling, including ``at-joined/1``. ``columns`` are the
    member's, with table-profile types.
    """
    if policy is not None:
        fields = tuple(policy["identity_fields"])
        # Native policies declare the useful row's identity independently of
        # source-processing schemas. One spelling preserves nulls, empty strings
        # and delimiter characters for every allowed policy identity.
        kinds = dict(columns)
        spelling = KeySpelling("native-tuple", "1", fields)
        declared_spelling(spelling, tuple(kinds[field] for field in fields))
        return spelling
    contract = TABLE_CONTRACTS.get(table)
    artifact = fields = description.get("identity")
    name = None
    if contract is not None and (fields is None or fields == list(contract.identity)):
        fields, name = list(contract.identity), getattr(contract, "key_spelling", None)
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
    if name not in KEY_SPELLINGS:
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


def _check_partition(connection, path, footer, partition):
    """Refuse a split member whose rows hold other values than its key names, compared as text.

    Every row group's footer statistics showing no nulls and min = max = the
    value prove it without reading data; a member without such statistics has
    that one column scanned instead. Statistics only ever prove agreement.
    """
    for column, value in partition.items():
        groups = footer.statistics.get(column, ())
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
    members compared in key order. Only footers and statistics are read, each
    fact for all of the table's members in one call.
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
    partitions = []
    for member in members:
        if member.role != "table" or member.media_type != _PARQUET or type(member.record_count) is not int:
            raise IntegrityError("generation table descriptor differs from its member")
    paths = [staging.joinpath(*member.object_key.split("/")) for member in members]
    footers = parquet_footers(connection, paths, statistics=partitioning)
    for member, path in zip(members, paths, strict=True):
        footer = footers[str(path)]
        if footer.rows != member.record_count:
            raise IntegrityError("Parquet footer row count differs from its descriptor")
        if [list(column) for column in footer.columns] != declared:
            raise IntegrityError("Parquet footer schema differs from its descriptor")
        if split:
            partitions.append(_partition(name, member.object_key, partitioning))
            _check_partition(connection, path, footer, partitions[-1])
    columns = profile_columns(footers[str(paths[0])].columns)
    if type(description.get("rows")) is not int or sum(member.record_count for member in members) != description["rows"]:
        raise IntegrityError("generation table descriptor differs from its member")
    _check_index(name, description, members, indexed, partitioning)
    return columns, tuple(partitioning)


def _check_index(name, description, members, indexed, partitioning):
    """Check the mutable publication selection using pinned metadata only."""
    if not members:
        raise IntegrityError("generation table has no local members")
    if indexed is not None:
        split = "partitionColumns" in description
        partitions = [_partition(name, m.object_key, partitioning) for m in members] if split else []
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
            fields = sorted(key for key in entry.keys() | expected.keys() if entry.get(key) != expected.get(key))
            raise IntegrityError(f"publication table descriptor differs from its pinned member: {name} {', '.join(fields)}")


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


def _fetch(fetcher, candidate, limit):
    """Open one public read, retrying a transient answer; the retried request has sent no byte to the caller."""
    for attempt in range(1, _RETRY_ATTEMPTS + 1):
        try:
            return fetcher.fetch(candidate, max_bytes=limit, task_id="generation-admission", attempt_id="stage")
        except HttpsRetryableResponseError as error:
            wait = _RETRY_BACKOFF_SECONDS * 2 ** (attempt - 1) if error.retry_after is None else error.retry_after
            if error.status not in _TRANSIENT_STATUSES or attempt == _RETRY_ATTEMPTS or wait > _RETRY_MAX_WAIT_SECONDS:
                raise
        sleep(wait)


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
                with _fetch(fetcher, candidate, limit) as stream:
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


def _stage(chunks, prefix, staging, root_bytes, manifest_bytes, max_bytes):
    """Copy declared members only; shared admission owns their hash verification."""
    root = _mapping(parse_canonical_json(root_bytes), "generation root")
    manifests = root.get("memberManifests")
    if not isinstance(manifests, list) or len(manifests) != 1 or manifests[0].get("objectKey") != "members.json":
        raise IntegrityError("generation requires one members.json manifest")
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
def resolve_generation(source, *, family, table, expected_pin=None):
    """Resolve the current pointer and bounded pin metadata without reading payload members."""
    if not isinstance(family, str) or not family or not isinstance(table, str) or not table or "/" in table or "\\" in table:
        raise ValueError("family and logical table name must be nonempty strings")
    validate_object_key(table + ".parquet", path="generation table")
    try:
        with _reader(source) as (chunks, remote):
            indexed, indexed_receipts, prefix = None, None, ""
            direct = not remote and (Path(source) / "artifact.json").exists()
            if not direct:
                publication = _publication(chunks)
                families = _mapping(publication.get("families"), "publication families")
                if family not in families:
                    raise IntegrityError(f"publication lists no family {family!r}")
                selected = _mapping(families[family], "publication family")
                pin = ArtifactPin(selected["logicalId"], selected["artifactDigest"])
                prefix = validate_object_key(selected["prefix"], path="generation prefix") + "/"
                indexed = _mapping(selected.get("tables"), "publication tables")
                indexed_receipts = selected.get("etlReceipts")
                if expected_pin is not None and pin != expected_pin:
                    raise IntegrityError("publication differs from the expected generation pin")
            root_bytes = _read(chunks, prefix + "artifact.json")
            root = _mapping(parse_canonical_json(root_bytes), "generation root")
            if direct:
                pin = expected_pin or ArtifactPin(root["logicalId"], root["artifactDigest"])
            manifest_bytes = _read(chunks, prefix + "members.json")
            yield GenerationSource(chunks, prefix, direct, pin, root_bytes, manifest_bytes, indexed, indexed_receipts)
    except (ArtifactVerificationError, MemberSourceError, KeyError, TypeError, ValueError, OSError, duckdb.Error) as error:
        raise IntegrityError(f"generation admission refused: {error}") from error


@contextmanager
def stage_generation(source, *, family, table, directory=None, expected_pin=None, max_bytes=_STAGED_BYTES):
    """Stage and independently validate every member of a complete producer family.

    The runtime may supply an already resolved source after checking its own
    retained admission. Public callers otherwise resolve the source here.
    Producer files are never moved or modified.
    """
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be positive")
    filename = validate_object_key(table + ".parquet", path="generation table")
    try:
        with ExitStack() as stack:
            if isinstance(source, GenerationSource):
                resolved = source
                if expected_pin is not None and resolved.pin != expected_pin:
                    raise IntegrityError("publication differs from the expected generation pin")
            else:
                resolved = stack.enter_context(resolve_generation(source, family=family, table=table, expected_pin=expected_pin))
            chunks, prefix, direct, pin = resolved.chunks, resolved.prefix, resolved.direct, resolved.pin
            root_bytes, indexed, indexed_receipts = resolved.root_bytes, resolved.indexed, resolved.indexed_receipts
            staging = Path(stack.enter_context(TemporaryDirectory(prefix="generation-", dir=directory)))
            manifest_bytes = _stage(chunks, prefix, staging, root_bytes, resolved.manifest_bytes, max_bytes)
            result = {}
            def verify(artifact, member_source):
                spec = _mapping(artifact.root.get("spec"), "generation spec")
                if artifact.root["kind"] != "spicy-regs-rollup-generation" or spec.get("family") != family or spec.get("publicationStatus") != "complete-family":
                    raise IntegrityError("admission requires the selected complete-family rollup generation")
                tables = _mapping(spec.get("tables"), "generation tables")
                grouped = {}
                for member in iter_member_descriptors(artifact, member_source):
                    grouped.setdefault(_member_table(member.object_key), []).append(member)
                receipt_declaration = spec.get("etlReceipts")
                receipts = ()
                policies = {}
                if receipt_declaration is not None:
                    from docspec.adapters.generation_receipts import KEY, verify_receipts

                    receipt_members = grouped.pop(KEY, [])
                    if (len(receipt_members) != 1 or receipt_members[0].object_key != KEY
                            or receipt_members[0].role != "table" or receipt_members[0].media_type != _PARQUET):
                        raise IntegrityError("Generation must name one shared ETL receipt member")
                    if not direct and indexed_receipts is None:
                        raise IntegrityError("Publication does not select the pinned receipt member")
                    verify_receipts(staging, receipt_declaration, tables, grouped, receipt_members[0], indexed=indexed_receipts)
                    policies = {policy["dataset"]: policy for policy in receipt_declaration["policies"]}
                    receipts = tuple(StagedMember(staging / member.object_key, member) for member in receipt_members)
                elif indexed_receipts is not None:
                    raise IntegrityError("Publication names receipts absent from the artifact")
                result["receipts"] = receipts
                if set(tables) != set(grouped) or filename not in grouped or indexed is not None and set(indexed) != set(tables):
                    raise IntegrityError("generation table set differs from its members or publication index")
                with duckdb.connect() as connection:
                    for name, members in grouped.items():
                        members.sort(key=lambda member: member.object_key)
                        description = _mapping(tables[name], "generation table")
                        canonical, partitioning = _check_table(connection, staging, name, description, members, indexed)
                        if name == filename:
                            if not policies:
                                _contract_types(table, canonical)
                            result.update(members=tuple(StagedMember(staging.joinpath(*member.object_key.split("/")), member)
                                                        for member in members),
                                          partition_columns=partitioning, columns=canonical,
                                          record_count=description["rows"], key=_key_rule(family, table, description, canonical, policies.get(table)))
            artifact = admit_artifact(LocalMemberSource(staging), expected_pin=pin, root_byte_limit=_DOCUMENT_BYTES,
                                      manifest_byte_limit=_DOCUMENT_BYTES, semantic_verifier=verify)
            yield AdmittedGeneration(pin=artifact.pin, root_bytes=root_bytes, manifest_bytes=manifest_bytes, **result)
    except (ArtifactVerificationError, MemberSourceError, KeyError, TypeError, ValueError, OSError, duckdb.Error) as error:
        raise IntegrityError(f"generation admission refused: {error}") from error
