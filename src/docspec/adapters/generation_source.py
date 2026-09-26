"""Stage and admit a complete producer generation before publishing a table state."""

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit

import duckdb
import pyarrow.parquet as pq
from rulespec_artifacts import (ArtifactPin, ArtifactVerificationError, LocalMemberSource,
    MemberDescriptor, MemberSourceError, admit_artifact, iter_member_descriptors,
    parse_canonical_json, validate_object_key)
from spicy_docs.schemas import TABLE_CONTRACTS

from docspec.adapters.content_fetchers.https import HttpsContentFetcher, HttpsContentFetcherConfig
from docspec.adapters.storage.records import native_columns
from docspec.domain.content import CandidateFile
from docspec.domain.table_rows import KeySpelling
from docspec.errors import IntegrityError, LimitExceededError


_DOCUMENT_BYTES = 8 * 1024**2
_STAGED_BYTES = 64 * 1024**3
_CHUNK_BYTES = 1024**2


@dataclass(frozen=True)
class AdmittedGeneration:
    """The selected table and its exact source evidence, valid inside the staging context.

    ``columns`` names each column with its table-profile type, ready for a
    TableSchema; ``key`` is the declared member-key spelling.
    """
    path: Path
    pin: ArtifactPin
    root_bytes: bytes
    manifest_bytes: bytes
    member: MemberDescriptor
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


def _pointer(payload):
    try:
        value = json.loads(payload, object_pairs_hook=_unique)
    except (ValueError, UnicodeError) as error:
        raise IntegrityError("generation pointer is not valid JSON") from error
    value = _mapping(value, "publication")
    if value.get("format") != "spicy-regs-publication" or type(value.get("version")) is not int or value["version"] != 1:
        raise IntegrityError("unsupported generation publication pointer")
    return value


def _key_rule(family, table, description, columns):
    fields = description.get("identity")
    if fields is None:
        contract = TABLE_CONTRACTS.get(table)
        fields = None if contract is None else contract.identity
    if fields is None and (family, table) == ("federal-register", "federal_register"):
        fields = ("document_number", "publication_date")
    if not isinstance(fields, (list, tuple)) or not fields or any(not isinstance(field, str) or not field for field in fields):
        raise IntegrityError("table has no declared identity fields")
    fields = tuple(fields)
    if len(set(fields)) != len(fields) or not set(fields) <= {name for name, _ in columns}:
        raise IntegrityError("table identity fields differ from its columns")
    if (family, table, fields) == ("federal-register", "federal_register", ("document_number", "publication_date")):
        return KeySpelling("federal-register-source-record-id", "1", fields)
    if len(fields) == 1:
        return KeySpelling("value", "1", fields)
    # The installed provider has no versioned composite key declarations.
    # A provisional join would silently change identity when one is introduced.
    raise IntegrityError("composite table identity requires a versioned spicy-docs key spelling")


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
    supplies expected_pin. A publication base supplies an untrusted pinned pointer.
    Every family member is staged and checked by Rulespec admission; only the
    selected table is returned. Producer files are never moved or modified.
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
                publication = _pointer(_read(chunks, "publication.json"))
                selected = _mapping(_mapping(publication.get("families"), "publication families").get(family), "publication family")
                pin = ArtifactPin(selected["logicalId"], selected["artifactDigest"])
                prefix = validate_object_key(selected["prefix"], path="generation prefix") + "/"
                indexed = _mapping(selected.get("tables"), "publication tables")
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
                members = {member.object_key: member for member in iter_member_descriptors(artifact, member_source)}
                if set(tables) != set(members) or filename not in members or indexed is not None and set(indexed) != set(tables):
                    raise IntegrityError("generation table set differs from its members or publication index")
                for name, member in members.items():
                    description = _mapping(tables[name], "generation table")
                    if member.role != "table" or member.media_type != "application/vnd.apache.parquet" or type(description.get("rows")) is not int or member.record_count != description["rows"]:
                        raise IntegrityError("generation table descriptor differs from its member")
                    if indexed is not None and indexed[name] != {**description, "byteSize": member.byte_size, "sha256": member.sha256}:
                        raise IntegrityError("publication table descriptor differs from its pinned member")
                    path = staging.joinpath(*name.split("/"))
                    with pq.ParquetFile(path) as parquet:
                        if parquet.metadata.num_rows != description["rows"]:
                            raise IntegrityError("Parquet footer row count differs from its descriptor")
                    with duckdb.connect() as connection:
                        footer = connection.read_parquet(str(path))
                        columns = tuple(zip(footer.columns, map(str, footer.types), strict=True))
                        canonical = native_columns(footer)
                    if [list(column) for column in columns] != description.get("columns"):
                        raise IntegrityError("Parquet footer schema differs from its descriptor")
                    if name == filename:
                        result.update(path=path, member=member, columns=canonical, record_count=description["rows"],
                                      key=_key_rule(family, table, description, columns))
            artifact = admit_artifact(LocalMemberSource(staging), expected_pin=pin, root_byte_limit=_DOCUMENT_BYTES,
                                      manifest_byte_limit=_DOCUMENT_BYTES, semantic_verifier=verify)
            yield AdmittedGeneration(pin=artifact.pin, root_bytes=root_bytes, manifest_bytes=manifest_bytes, **result)
    except (ArtifactVerificationError, MemberSourceError, KeyError, TypeError, ValueError, OSError, duckdb.Error) as error:
        raise IntegrityError(f"generation admission refused: {error}") from error
