"""SQLite authority for Core metadata, behind bounded publication units.

The application publisher owns content readiness, dependency adequacy and PROV
admission. This backend owns atomic visibility, immutable rows, version checks,
snapshots and retry identities. Recording an outcome alone never retains it.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from contextlib import contextmanager, closing, nullcontext
from pathlib import Path
import os
import hashlib
import stat
from threading import local
import sqlite3
import tempfile
from typing import Any

from docspec.adapters.locks import lock_descriptor
from docspec.adapters.storage.files import _contained, _storage_root, _sync_parents
from docspec.adapters.storage.provenance import PROVENANCE_SCHEMA, admit_provenance
from docspec.ports.record_storage import bounded_rows
from docspec.adapters.streams import BATCH_BYTES, BATCH_ROWS, owned_iterator
from docspec.domain.core import RECORD_ID_FIELDS
from docspec.domain.core_admission import admit_record, record_parts, record_value
from docspec.domain.references import BlobRef, LayerRef
from docspec.domain.streams import bounded_items
from docspec.ports.record_storage import RecordStorage
from docspec.domain.identity import decode_canonical_json_value, canonical_value_bytes, require_sha256, require_text, sha256_digest
from docspec.errors import IntegrityError, LimitExceededError, StaleBaseError, StateTransitionError
from docspec.ports.core_ledger import CandidateMatch, MetadataBatch, MetadataLink, RecordKey, StoredRecord, RemovalContent, RemovalOutcome


_APPLICATION_ID = 0x44535043
_VERSION = 1
_KINDS = ",".join(f"'{kind}'" for kind in RECORD_ID_FIELDS)
_SCHEMA = (
    "CREATE TABLE record_layers (layer_id TEXT PRIMARY KEY,reference BLOB NOT NULL) WITHOUT ROWID",
    f"""CREATE TABLE records (
        kind TEXT NOT NULL CHECK(kind IN ({_KINDS})), record_id TEXT NOT NULL CHECK(length(record_id)>0),
        payload BLOB, outcome TEXT CHECK(outcome IN ('success','failed','interrupted','incomplete')),
        row_digest TEXT NOT NULL, source_layer TEXT REFERENCES record_layers(layer_id), byte_size INTEGER NOT NULL CHECK(byte_size>=0),
        PRIMARY KEY(kind,record_id), CHECK((kind='result')=(outcome IS NOT NULL)),
        CHECK((payload IS NULL)=(source_layer IS NOT NULL)), CHECK(source_layer IS NULL OR kind='entity')
    ) WITHOUT ROWID""",
    "CREATE UNIQUE INDEX data_identity ON records(record_id) WHERE kind IN ('entity','state')",
    "CREATE INDEX executions_request ON records(json_extract(payload, '$.request_id')) WHERE kind='execution'",
    """CREATE TABLE units (unit_id TEXT PRIMARY KEY CHECK(length(unit_id)>0), operation TEXT NOT NULL, digest TEXT NOT NULL) WITHOUT ROWID""",
    """CREATE TABLE retention (
        kind TEXT NOT NULL, record_id TEXT NOT NULL, unit_id TEXT NOT NULL REFERENCES units(unit_id),
        available INTEGER NOT NULL CHECK(available IN (0,1)), evidence_version INTEGER NOT NULL DEFAULT 0 CHECK(evidence_version>=0),
        PRIMARY KEY(kind,record_id), FOREIGN KEY(kind,record_id) REFERENCES records(kind,record_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE links (
        owner_kind TEXT NOT NULL, owner_id TEXT NOT NULL, relation TEXT NOT NULL, label TEXT NOT NULL,
        target_kind TEXT NOT NULL, target_id TEXT NOT NULL,
        PRIMARY KEY(owner_kind,owner_id,relation,label,target_kind,target_id),
        FOREIGN KEY(owner_kind,owner_id) REFERENCES records(kind,record_id),
        FOREIGN KEY(target_kind,target_id) REFERENCES records(kind,record_id)
    ) WITHOUT ROWID""",
    "CREATE INDEX links_target ON links(target_kind,target_id,relation)",
    """CREATE TABLE candidates (
        digest TEXT NOT NULL, result_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'result' CHECK(kind='result'),
        PRIMARY KEY(digest,result_id), FOREIGN KEY(kind,result_id) REFERENCES retention(kind,record_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE heads (
        dataset TEXT PRIMARY KEY, kind TEXT NOT NULL CHECK(kind IN ('state','result')), record_id TEXT NOT NULL,
        update_id TEXT NOT NULL REFERENCES units(unit_id), FOREIGN KEY(kind,record_id) REFERENCES retention(kind,record_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE progress (
        sequence INTEGER PRIMARY KEY, update_id TEXT NOT NULL UNIQUE REFERENCES units(unit_id),
        execution_id TEXT NOT NULL, kind TEXT NOT NULL DEFAULT 'execution' CHECK(kind='execution'),
        status TEXT NOT NULL CHECK(status IN ('started','progress','failed','interrupted','incomplete')),
        payload BLOB NOT NULL, FOREIGN KEY(kind,execution_id) REFERENCES records(kind,record_id)
    )""",
    "CREATE INDEX progress_execution ON progress(execution_id,sequence)",
    """CREATE TABLE removals (
        update_id TEXT PRIMARY KEY REFERENCES units(unit_id), policy_id TEXT NOT NULL,
        kind TEXT NOT NULL DEFAULT 'retention_policy' CHECK(kind='retention_policy'),
        complete INTEGER NOT NULL DEFAULT 0 CHECK(complete IN (0,1)),
        FOREIGN KEY(kind,policy_id) REFERENCES records(kind,record_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE removal_targets (
        update_id TEXT NOT NULL REFERENCES removals(update_id), kind TEXT NOT NULL, record_id TEXT NOT NULL,
        PRIMARY KEY(update_id,kind,record_id), FOREIGN KEY(kind,record_id) REFERENCES retention(kind,record_id)
    ) WITHOUT ROWID""",
    """CREATE TABLE removal_content (
        update_id TEXT NOT NULL REFERENCES removals(update_id), store TEXT NOT NULL CHECK(store IN ('blobs','records')),
        locator TEXT NOT NULL, reference BLOB NOT NULL,
        status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','deleted','absent','retained','failed')),
        error TEXT, PRIMARY KEY(update_id,store,locator)
    ) WITHOUT ROWID""",
) + PROVENANCE_SCHEMA


def _key(value: RecordKey) -> RecordKey:
    if not isinstance(value, (tuple, list)) or len(value) != 2 or not isinstance(value[0], str) or value[0] not in RECORD_ID_FIELDS:
        raise IntegrityError("invalid metadata record key")
    return value[0], require_text(value[1], "record identity")


def _encode(value) -> bytes:
    try:
        return canonical_value_bytes(value)
    except (TypeError, ValueError) as error:
        raise IntegrityError(f"metadata is outside the canonical JSON domain: {error}") from error


def _database_error(error: sqlite3.Error):
    code = getattr(error, "sqlite_errorcode", 0) & 255
    if code in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
        raise StateTransitionError("metadata database is busy; retry with the same update identity") from error
    raise IntegrityError(f"Core metadata operation failed: {error}") from error


def _row_size(row) -> int:
    return sum(len(value) if isinstance(value, bytes) else len(value.encode("utf-8")) if isinstance(value, str) else 8 for value in row)


def _collect(values) -> tuple:
    """A write unit is bounded; callers split large operations into explicit units."""
    return bounded_items(values, limit=BATCH_ROWS)


def _collect_parameters(values, normalize) -> tuple:
    """Bound a complete retention description by bytes, not new-record count."""
    rows, size = [], 0
    with owned_iterator(values) as source:
        for value in source:
            row = normalize(value)
            size += _row_size(row)
            if size > BATCH_BYTES:
                raise LimitExceededError("metadata parameters exceed the 8 MiB limit")
            rows.append(row)
    return tuple(rows)


def _bind(connection, statement, rows):
    with owned_iterator(bounded_rows(rows, size=_row_size)) as batches:
        for batch in batches:
            connection.executemany(statement, batch)


class LocalSqliteCoreLedger:
    def __init__(self, path: Path, *, busy_timeout_ms: int = 5000, create: bool = True, record_storage: RecordStorage | None = None, read_only: bool = False) -> None:
        if type(busy_timeout_ms) is not int or busy_timeout_ms <= 0:
            raise ValueError("metadata busy timeout must be a positive integer")
        create = create and not read_only
        root = _storage_root(Path(path).parent, create=create)
        self.path = _contained(root, Path(path).name)
        self.busy_timeout_ms = busy_timeout_ms
        self.record_storage = record_storage
        self.read_only = read_only
        self._closed = False
        self._guard = local()
        connection = self._open(create=create)
        try:
            if self.read_only:
                self._check_identity(connection)
                return
            version, application = self._identity(connection)
            if (version, application) != (_VERSION, _APPLICATION_ID):
                if not create or (version, application) != (0, 0) or connection.execute(
                    "SELECT 1 FROM sqlite_schema WHERE name NOT LIKE 'sqlite_%' LIMIT 1"
                ).fetchone():
                    raise IntegrityError("unsupported Core metadata database or schema version")
            if connection.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
                raise IntegrityError("Core metadata requires SQLite WAL mode")
            connection.execute("BEGIN IMMEDIATE")
            # Another constructor may have initialized the empty database meanwhile.
            if self._identity(connection) == (0, 0):
                for statement in _SCHEMA:
                    connection.execute(statement)
                connection.execute(f"PRAGMA application_id={_APPLICATION_ID}")
                connection.execute(f"PRAGMA user_version={_VERSION}")
            self._check_identity(connection)
            connection.commit()
            _sync_parents(root, self.path)
        except sqlite3.Error as error:
            connection.rollback()
            _database_error(error)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def content_guard(self, *, exclusive: bool = False):
        """Protect content through publication; cleanup owns the exclusive scope.

        Nested calls reuse the owning thread's guard. A shared scope cannot be
        upgraded; callers acquire cleanup protection before reading its plan.
        """
        if self._closed:
            raise StateTransitionError("metadata backend is closed")
        if self.read_only:
            if exclusive:
                raise StateTransitionError("exported metadata is read-only")
            yield
            return
        held = getattr(self._guard, "exclusive", None)
        if held is not None:
            if exclusive and not held:
                raise StateTransitionError("cannot upgrade publication protection to cleanup")
            yield
            return
        with self._file_guard(".content.lock", shared=not exclusive, label="content-protection",
                busy_message="content is protected by another publication or cleanup; retry later"):
            self._guard.exclusive = exclusive
            try:
                # Cleanup mutates physical availability inside its exclusive
                # guard, so only shared publication scopes reuse admission.
                with self.record_storage.admission_scope() if self.record_storage is not None and not exclusive else nullcontext():
                    yield
            finally:
                del self._guard.exclusive

    def request_guard(self, request_id: str):
        """Serialize exact-request retries without blocking other publications."""
        require_text(request_id, "request identity")
        name = hashlib.sha256(request_id.encode("utf-8")).hexdigest()
        return self._file_guard(".request-" + name + ".lock", label="request",
                                busy_message="this request is running; retry with the same batch ID")

    @contextmanager
    def _file_guard(self, suffix, *, label, busy_message, shared=False):
        path = _contained(self.path.parent, self.path.name + suffix)
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
        try:
            opened = os.fstat(descriptor)
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
                raise IntegrityError(f"{label} lock must be a regular file with one link")
            with lock_descriptor(descriptor, shared=shared, busy_message=busy_message):
                current = path.stat(follow_symlinks=False)
                if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                    raise IntegrityError(f"{label} lock changed during acquisition")
                yield
        finally:
            os.close(descriptor)

    def _open(self, *, create: bool = False) -> sqlite3.Connection:
        if self._closed:
            raise StateTransitionError("metadata backend is closed")
        if self.path.is_symlink():
            raise IntegrityError("metadata path must not be a symlink")
        try:
            connection = sqlite3.connect(
                self.path.as_uri() + ("?mode=ro&immutable=1" if self.read_only else "?mode=rwc" if create else "?mode=rw"), uri=True,
                timeout=self.busy_timeout_ms / 1000, isolation_level=None,
            )
        except sqlite3.Error as error:
            _database_error(error)
        try:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA synchronous=FULL")
            connection.execute("PRAGMA temp_store=FILE")
            connection.execute("PRAGMA cache_size=-8192")
            return connection
        except BaseException:
            connection.close()
            raise

    @staticmethod
    def _identity(connection) -> tuple[int, int]:
        return connection.execute("PRAGMA user_version").fetchone()[0], connection.execute("PRAGMA application_id").fetchone()[0]

    def _check_identity(self, connection) -> None:
        if self._identity(connection) != (_VERSION, _APPLICATION_ID):
            raise IntegrityError("unsupported Core metadata database or schema version")

    @contextmanager
    def _transaction(self, *, write: bool = False):
        if write and self.read_only:
            raise StateTransitionError("exported metadata is read-only")
        connection = self._open()
        try:
            self._check_identity(connection)
            connection.execute("BEGIN IMMEDIATE" if write else "BEGIN")
            yield connection
            connection.commit()
        except sqlite3.Error as error:
            connection.rollback()
            _database_error(error)
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    def verify_snapshot(self) -> None:
        """Re-admit imported rows and rebuild provenance with the ordinary owner."""
        with self._transaction() as connection:
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",) or connection.execute("PRAGMA foreign_key_check").fetchone():
                raise IntegrityError("exported metadata has invalid database relationships")
            keys = connection.execute("SELECT kind,record_id FROM records ORDER BY kind,record_id")
            for _ in self.read_records(keys):
                pass
        with tempfile.TemporaryDirectory(prefix="docspec-export-check-") as directory:
            with self._transaction() as source, closing(sqlite3.connect(Path(directory) / "metadata.sqlite", uri=True)) as check:
                source.backup(check)
                check.execute("DELETE FROM provenance_events")
                check.execute("DELETE FROM derivations")
                check.commit()
                keys = source.execute("SELECT kind,record_id FROM records WHERE kind='result' ORDER BY record_id")
                for batch in self.read_records(keys):
                    check.execute("DROP TABLE IF EXISTS temp.incoming_events")
                    admit_provenance(check, (record_value(row.value) for row in batch if row.value is not None))
                check.execute("ATTACH DATABASE ? AS original", (self.path.as_uri() + "?mode=ro&immutable=1",))
                for table in ("provenance_events", "derivations"):
                    if (check.execute("SELECT * FROM " + table + " EXCEPT SELECT * FROM original." + table).fetchone()
                            or check.execute("SELECT * FROM original." + table + " EXCEPT SELECT * FROM " + table).fetchone()):
                        raise IntegrityError("exported provenance index differs from its admitted result statements")

    def export_snapshot(self, destination: Path, keys: Iterable[RecordKey], *, full: Iterable[RecordKey]) -> None:
        """Copy a selected metadata scope, preserving original receipt versions.

        The exporter installs equivalent compact state representations afterward.
        Explicit bulk entities become bounded inline rows; state populations are
        copied through the existing bulk state writer instead of a member graph.
        """
        destination = Path(destination)
        if destination.exists():
            raise IntegrityError("metadata snapshot destination already exists")
        destination.parent.mkdir(parents=True, exist_ok=True)
        with self.content_guard(), self._transaction() as source, closing(sqlite3.connect(destination)) as target:
            source.backup(target)
            target.execute("PRAGMA foreign_keys=OFF")
            target.execute("CREATE TEMP TABLE selected(kind TEXT,record_id TEXT,PRIMARY KEY(kind,record_id)) WITHOUT ROWID")
            target.executemany("INSERT OR IGNORE INTO selected VALUES (?,?)", keys)
            target.execute("CREATE TEMP TABLE full_values(kind TEXT,record_id TEXT,PRIMARY KEY(kind,record_id)) WITHOUT ROWID")
            target.executemany("INSERT OR IGNORE INTO full_values VALUES (?,?)", full)
            selected = "EXISTS(SELECT 1 FROM selected s WHERE s.kind=records.kind AND s.record_id=records.record_id)"
            target.execute("DELETE FROM links WHERE NOT EXISTS(SELECT 1 FROM selected s WHERE s.kind=owner_kind AND s.record_id=owner_id) OR NOT EXISTS(SELECT 1 FROM selected s WHERE s.kind=target_kind AND s.record_id=target_id)")
            for table in ("heads", "removal_content", "removal_targets", "removals"):
                target.execute("DELETE FROM " + table)
            target.execute("DELETE FROM candidates WHERE NOT EXISTS(SELECT 1 FROM selected WHERE kind='result' AND record_id=result_id)")
            target.execute("DELETE FROM progress WHERE NOT EXISTS(SELECT 1 FROM selected WHERE kind='execution' AND record_id=execution_id)")
            target.execute("DELETE FROM provenance_events WHERE NOT EXISTS(SELECT 1 FROM selected WHERE kind='execution' AND record_id=execution_id)")
            target.execute("DELETE FROM derivations WHERE NOT EXISTS(SELECT 1 FROM selected WHERE kind IN ('entity','state') AND record_id=child)")
            target.execute("DELETE FROM retention WHERE NOT EXISTS(SELECT 1 FROM selected s WHERE s.kind=retention.kind AND s.record_id=retention.record_id)")
            target.execute("DELETE FROM records WHERE NOT " + selected)
            target.execute("UPDATE retention SET available=0 WHERE kind IN ('entity','state','selected_value','state_representation','revision') AND NOT EXISTS(SELECT 1 FROM full_values f WHERE f.kind=retention.kind AND f.record_id=retention.record_id)")
            for batch in self.read_records(target.execute("SELECT kind,record_id FROM selected WHERE kind='entity' ORDER BY record_id")):
                for row in batch:
                    if row is not None and row.value is not None:
                        target.execute("UPDATE records SET payload=?,source_layer=NULL WHERE kind=? AND record_id=?",
                                       (canonical_value_bytes(record_value(row.value)), *row.key))
            target.execute("DELETE FROM record_layers WHERE layer_id NOT IN (SELECT source_layer FROM records WHERE source_layer IS NOT NULL)")
            target.execute("DELETE FROM units WHERE unit_id NOT IN (SELECT unit_id FROM retention UNION SELECT update_id FROM progress)")
            target.commit()
            target.execute("PRAGMA journal_mode=DELETE")
            target.execute("VACUUM")
            if target.execute("PRAGMA foreign_key_check").fetchone():
                raise IntegrityError("selected metadata snapshot has broken references")

    def close(self) -> None:
        """Refuse new operations; exhaust or close owned readers before shutdown."""
        self._closed = True

    @staticmethod
    def _unit(connection, unit_id: str, operation: str, payload: bytes) -> bool:
        require_text(unit_id, "metadata update identity")
        if len(payload) > BATCH_BYTES:
            raise LimitExceededError("metadata update exceeds the 8 MiB limit")
        digest = sha256_digest(payload)
        existing = connection.execute("SELECT operation,digest FROM units WHERE unit_id=?", (unit_id,)).fetchone()
        if existing is not None:
            if existing != (operation, digest):
                raise IntegrityError("metadata update identity conflicts with its retained operation")
            return False
        connection.execute("INSERT INTO units VALUES (?,?,?)", (unit_id, operation, digest))
        return True

    @staticmethod
    def _requests(connection, values, *, names: tuple[str, ...], normalize):
        # Only fixed call-site names enter SQL. Request values always use parameters.
        connection.execute(f"CREATE TEMP TABLE wanted (ordinal INTEGER PRIMARY KEY,{','.join(name + ' TEXT NOT NULL' for name in names)})")
        ordinal = 0
        with owned_iterator(values) as source:
            parameters = (normalize(value) for value in source)
            with owned_iterator(bounded_rows(parameters, size=_row_size)) as chunks:
                for chunk in chunks:
                    connection.executemany(
                        f"INSERT INTO wanted VALUES ({','.join('?' for _ in range(len(names) + 1))})",
                        ((ordinal + index, *row) for index, row in enumerate(chunk)),
                    )
                    ordinal += len(chunk)

    def commit(self, batch: MetadataBatch) -> bool:
        records, evidence, results = {}, {}, {}
        size = 0
        for record in _collect(batch.records):
            value, payload = record_parts(record)
            kind = value["kind"]
            key = kind, value[RECORD_ID_FIELDS[kind]]
            size += len(payload)
            if size > BATCH_BYTES:
                raise LimitExceededError("metadata publication unit exceeds the 8 MiB limit")
            if batch.record_layer is not None and (kind != "entity" or self.record_storage is None):
                raise IntegrityError("bulk entity metadata requires its record store and entity-only rows")
            row = (*key, None if batch.record_layer else payload,
                   value["outcome"]["status"] if kind == "result" else None,
                   sha256_digest(payload), None if batch.record_layer is None else batch.record_layer.layer_id, len(payload))
            if key in records and records[key] != row:
                raise IntegrityError("metadata unit contains conflicting record identities")
            records[key] = row
            if kind == "result":
                results[key[1]] = value
            if kind == "dependency_evidence":
                evidence[key[1]] = value["result_id"]
        retained = sorted(set(_collect_parameters(batch.retained, _key)))
        candidates = sorted(set((require_sha256(digest), require_text(result, "candidate result")) for digest, result in _collect(batch.candidates)))
        links = sorted(set(_collect_parameters(batch.links, lambda link: (
            *_key(link.owner), require_text(link.relation, "link relation"),
            require_text(link.label, "link label"), *_key(link.target)))))
        preferred = {}
        for owner_kind, owner_id, relation, label, target_kind, target_id in links:
            if relation == "representation":
                if owner_kind != "state" or target_kind != "state_representation" or label != "representation":
                    raise IntegrityError("preferred representation requires its state and representation")
                if owner_id in preferred and preferred[owner_id] != target_id:
                    raise IntegrityError("state publication names multiple preferred representations")
                preferred[owner_id] = target_id
        def expected(item):
            key, version = item
            if type(version) is not int or version < 0:
                raise IntegrityError("invalid expected metadata evidence version")
            return (*_key(key), version)
        versions = _collect_parameters(batch.expected_versions, expected)
        receipt = _encode({
            "records": [[*key, row[4]] for key, row in sorted(records.items())],
            "retained": [list(key) for key in retained], "candidates": [list(row) for row in candidates],
            # Snapshot guards apply to a new write, not the identity of an
            # already committed unit whose historical success is being retried.
            "links": [list(row) for row in links],
        })
        if size + len(receipt) + sum(map(_row_size, versions)) > BATCH_BYTES:
            raise LimitExceededError("metadata publication unit exceeds the 8 MiB limit")
        with self.content_guard(), self._transaction(write=True) as connection:
            if not self._unit(connection, batch.unit_id, "commit", receipt):
                return False
            connection.execute("CREATE TEMP TABLE incoming AS SELECT * FROM records WHERE 0")
            if batch.record_layer is not None:
                reference = _encode(batch.record_layer.to_dict())
                existing = connection.execute("SELECT reference FROM record_layers WHERE layer_id=?", (batch.record_layer.layer_id,)).fetchone()
                if existing is not None and existing[0] != reference:
                    raise IntegrityError("record layer identity conflicts with its retained reference")
                connection.execute("INSERT INTO record_layers VALUES (?,?) ON CONFLICT DO NOTHING", (batch.record_layer.layer_id, reference))
            _bind(connection, "INSERT INTO incoming VALUES (?,?,?,?,?,?,?)", records.values())
            if connection.execute(
                "SELECT 1 FROM incoming i JOIN records r USING(kind,record_id) WHERE i.row_digest!=r.row_digest LIMIT 1"
            ).fetchone():
                raise IntegrityError("metadata record conflicts with its immutable identity")
            connection.execute("CREATE TEMP TABLE expected (kind TEXT,record_id TEXT,version INTEGER)")
            _bind(connection, "INSERT INTO expected VALUES (?,?,?)", versions)
            if connection.execute(
                "SELECT 1 FROM expected e LEFT JOIN retention r USING(kind,record_id) "
                "WHERE r.available IS NOT 1 OR r.evidence_version!=e.version LIMIT 1"
            ).fetchone():
                raise StaleBaseError("metadata availability or dependency evidence changed")
            existing_keys = set(connection.execute("SELECT r.kind,r.record_id FROM records r JOIN incoming i USING(kind,record_id)"))
            connection.execute("CREATE TEMP TABLE new_evidence (result_id TEXT NOT NULL,evidence_id TEXT PRIMARY KEY)")
            _bind(connection, "INSERT INTO new_evidence VALUES (?,?)", (
                (result_id, evidence_id) for evidence_id, result_id in evidence.items() if ("dependency_evidence", evidence_id) not in existing_keys
            ))
            connection.execute("CREATE INDEX new_evidence_result ON new_evidence(result_id)")
            connection.execute("INSERT INTO records SELECT * FROM incoming WHERE true ON CONFLICT DO NOTHING")
            if batch.record_layer is not None:
                # The immutable digest was checked above. A newly admitted
                # physical copy may replace its earlier storage location without
                # changing the entity or retaining a second SQLite payload.
                connection.execute(
                    "UPDATE records SET payload=NULL,source_layer=? WHERE (kind,record_id) IN (SELECT kind,record_id FROM incoming)",
                    (batch.record_layer.layer_id,),
                )
            else:
                # Restore reclaimed bulk rows from newly admitted inline bytes;
                # the original digest above still owns immutable identity.
                connection.execute(
                    "UPDATE records SET payload=(SELECT i.payload FROM incoming i WHERE i.kind=records.kind AND i.record_id=records.record_id),source_layer=NULL "
                    "WHERE source_layer IS NOT NULL AND (kind,record_id) IN (SELECT kind,record_id FROM incoming) "
                    "AND EXISTS(SELECT 1 FROM retention t WHERE t.kind=records.kind AND t.record_id=records.record_id AND t.available=0)"
                )
            admit_provenance(connection, (value for identity, value in results.items() if ("result", identity) not in existing_keys))
            connection.execute("CREATE TEMP TABLE retaining (kind TEXT,record_id TEXT,PRIMARY KEY(kind,record_id))")
            _bind(connection, "INSERT INTO retaining VALUES (?,?)", retained)
            connection.execute(
                "INSERT INTO retention(kind,record_id,unit_id,available) SELECT kind,record_id,?,1 FROM retaining WHERE true "
                "ON CONFLICT(kind,record_id) DO UPDATE SET available=1,evidence_version=evidence_version+1 WHERE available=0",
                (batch.unit_id,),
            )
            if connection.execute(
                "SELECT 1 FROM retaining t JOIN records r USING(kind,record_id) WHERE r.kind='result' AND r.outcome!='success' LIMIT 1"
            ).fetchone():
                raise IntegrityError("unsuccessful result cannot claim successful retention")
            # Equivalence is checked by the publisher before this transaction.
            # Only the preferred physical link moves; old records and content
            # remain retained until the policy owner authorizes their removal.
            _bind(connection, "DELETE FROM links WHERE owner_kind='state' AND owner_id=? AND relation='representation'",
                                   ((identity,) for identity in preferred))
            _bind(connection, "INSERT INTO links VALUES (?,?,?,?,?,?) ON CONFLICT DO NOTHING", links)
            if connection.execute(
                "SELECT 1 FROM new_evidence e LEFT JOIN retention r ON r.kind='result' AND r.record_id=e.result_id "
                "WHERE r.record_id IS NULL LIMIT 1"
            ).fetchone():
                raise IntegrityError("dependency evidence requires a retained result")
            connection.execute(
                "UPDATE retention SET evidence_version=evidence_version+(SELECT count(*) FROM new_evidence WHERE result_id=retention.record_id) "
                "WHERE kind='result' AND record_id IN (SELECT result_id FROM new_evidence)"
            )
            connection.execute(
                "INSERT INTO links SELECT 'result',result_id,'evidence',evidence_id,'dependency_evidence',evidence_id FROM new_evidence"
            )
            _bind(connection, "INSERT INTO candidates(digest,result_id) VALUES (?,?) ON CONFLICT DO NOTHING", candidates)
            return True

    def is_committed(self, unit_id: str) -> bool:
        require_text(unit_id, "metadata update identity")
        with self._transaction() as connection:
            return connection.execute("SELECT 1 FROM units WHERE unit_id=?", (unit_id,)).fetchone() is not None

    def read_records(self, keys: Iterable[RecordKey], *, include_values: bool = True) -> Iterator[tuple[StoredRecord | None, ...]]:
        with self._transaction() as connection:
            self._requests(connection, keys, names=("kind", "record_id"), normalize=_key)
            cursor = connection.execute(
                "SELECT w.kind,w.record_id,r.payload,t.unit_id,t.available,t.evidence_version,r.row_digest,l.reference,r.byte_size,r.outcome "
                "FROM wanted w LEFT JOIN records r USING(kind,record_id) "
                "LEFT JOIN retention t USING(kind,record_id) LEFT JOIN record_layers l ON l.layer_id=r.source_layer ORDER BY w.ordinal"
            )
            for rows in bounded_rows(cursor, size=lambda row: max(row[8] or 0, _row_size(row))):
                external, payloads, payload_bytes = {}, {}, 0
                for row in rows:
                    if include_values and row[7] is not None and row[4] != 0:
                        external.setdefault(row[7], set()).add(row[1])
                for encoded_reference, identities in external.items():
                    if self.record_storage is None:
                        raise IntegrityError("reading bulk entities requires the retained record store")
                    reference = LayerRef.from_dict(decode_canonical_json_value(encoded_reference, label="record layer reference"))
                    for batch in self.record_storage.lookup_batches(reference, sorted(identities)):
                        for identity, payload in zip(batch.column("record_identity").to_pylist(), batch.column("record_json").to_pylist(), strict=True):
                            payload_bytes += len(payload)
                            if payload_bytes > BATCH_BYTES:
                                raise LimitExceededError("retained metadata understates its external row byte sizes")
                            payloads[identity] = payload
                def resolved():
                    for row in rows:
                        if row[6] is None:
                            yield None
                            continue
                        if not include_values:
                            yield StoredRecord((row[0], row[1]), None, row[3] is not None, bool(row[4]), row[5] or 0, row[6])
                            continue
                        if row[7] is not None and row[4] == 0:
                            yield StoredRecord((row[0], row[1]), None, True, False, row[5] or 0, row[6])
                            continue
                        payload = row[2] if row[7] is None else payloads.get(row[1])
                        if payload is None or len(payload) != row[8] or sha256_digest(payload) != row[6]:
                            raise IntegrityError("retained entity row is missing or differs from its identity")
                        value = admit_record(payload)
                        declared = record_value(value)
                        if (declared["kind"], declared[RECORD_ID_FIELDS[declared["kind"]]]) != (row[0], row[1]):
                            raise IntegrityError("metadata key differs from its admitted record identity")
                        if row[0] == "result" and declared["outcome"]["status"] != row[9]:
                            raise IntegrityError("metadata outcome differs from its admitted result")
                        yield StoredRecord((row[0], row[1]), value, row[3] is not None, bool(row[4]), row[5] or 0, row[6])
                # External rows can be much larger than their SQLite pins.
                yield tuple(resolved())

    def find_candidates(self, requests: Iterable[tuple[str, str]]) -> Iterator[tuple[CandidateMatch, ...]]:
        with self._transaction() as connection:
            self._requests(connection, requests, names=("request_id", "digest"), normalize=lambda row: (
                require_text(row[0], "candidate request"), require_sha256(row[1]),
            ))
            cursor = connection.execute(
                "SELECT w.request_id,c.result_id,t.evidence_version FROM wanted w JOIN candidates c USING(digest) "
                "JOIN retention t ON t.kind='result' AND t.record_id=c.result_id "
                "JOIN records r ON r.kind=t.kind AND r.record_id=t.record_id "
                "WHERE t.available=1 AND r.outcome='success' ORDER BY w.ordinal,c.result_id"
            )
            for rows in bounded_rows(cursor, size=_row_size):
                yield tuple(CandidateMatch(*row) for row in rows)

    def read_links(self, keys: Iterable[RecordKey]) -> Iterator[tuple[MetadataLink, ...]]:
        with self._transaction() as connection:
            self._requests(connection, keys, names=("kind", "record_id"), normalize=_key)
            cursor = connection.execute(
                "SELECT l.owner_kind,l.owner_id,l.relation,l.label,l.target_kind,l.target_id FROM wanted w "
                "JOIN links l ON l.owner_kind=w.kind AND l.owner_id=w.record_id "
                "ORDER BY w.ordinal,l.relation,l.label,l.target_kind,l.target_id"
            )
            for rows in bounded_rows(cursor, size=_row_size):
                yield tuple(MetadataLink((row[0], row[1]), row[2], row[3], (row[4], row[5])) for row in rows)

    def read_dependencies(self, result_ids: Iterable[str]) -> Iterator[tuple[MetadataLink, ...]]:
        yield from self.read_links(("result", result_id) for result_id in result_ids)

    def affected_results(self, changed: Iterable[RecordKey]) -> Iterator[tuple[str, ...]]:
        """Find possible downstream work through declarations, not PROV events.

        UNION visits each record once even when conservative declarations cycle.
        Correspondence checks decide which of these results actually need work.
        """
        with self._transaction() as connection:
            self._requests(connection, changed, names=("kind", "record_id"), normalize=_key)
            cursor = connection.execute(
                "WITH RECURSIVE affected(kind,record_id) AS ("
                "SELECT kind,record_id FROM wanted "
                "UNION SELECT l.owner_kind,l.owner_id FROM affected a JOIN links l "
                "ON l.target_kind=a.kind AND l.target_id=a.record_id "
                "WHERE l.relation='dependency' AND l.owner_kind='result' "
                "UNION SELECT l.target_kind,l.target_id FROM affected a JOIN links l "
                "ON l.owner_kind=a.kind AND l.owner_id=a.record_id "
                "WHERE a.kind='result' AND l.relation='requires' AND l.label GLOB 'output:*'"
                ") SELECT a.record_id FROM affected a JOIN records r "
                "ON r.kind=a.kind AND r.record_id=a.record_id WHERE a.kind='result' ORDER BY a.record_id"
            )
            for rows in bounded_rows(cursor, size=_row_size):
                yield tuple(row[0] for row in rows)

    def executions(self, request_id: str) -> Iterator[tuple[str, ...]]:
        """Find actual attempts for an exact request without scanning payloads."""
        require_text(request_id, "request identity")
        with self._transaction() as connection:
            cursor = connection.execute(
                "SELECT record_id FROM records WHERE kind='execution' AND json_extract(payload, '$.request_id')=? ORDER BY record_id",
                (request_id,),
            )
            for rows in bounded_rows(cursor, size=_row_size):
                yield tuple(row[0] for row in rows)

    def record_progress(self, update_id: str, execution_id: str, status: str, description: dict[str, Any], *, expected_update_id: str | None = None) -> bool:
        require_text(execution_id, "execution identity")
        if not isinstance(status, str) or status not in {"started", "progress", "failed", "interrupted", "incomplete"}:
            raise IntegrityError("progress cannot assert successful retention")
        if type(description) is not dict:
            raise IntegrityError("progress description must be a JSON object")
        payload = _encode({"update_id": update_id, "execution_id": execution_id, "status": status, "description": description})
        with self._transaction(write=True) as connection:
            if not self._unit(connection, update_id, "progress", payload):
                return False
            if expected_update_id is not None:
                previous = connection.execute("SELECT update_id FROM progress WHERE execution_id=? ORDER BY sequence DESC LIMIT 1", (execution_id,)).fetchone()
                if previous != (expected_update_id,):
                    raise StaleBaseError("operation progress changed before its continuation claim")
            connection.execute("INSERT INTO progress(update_id,execution_id,status,payload) VALUES (?,?,?,?)", (update_id, execution_id, status, payload))
            return True

    def read_progress(self, execution_id: str) -> Iterator[tuple[bytes, ...]]:
        require_text(execution_id, "execution identity")
        with self._transaction() as connection:
            cursor = connection.execute("SELECT payload FROM progress WHERE execution_id=? ORDER BY sequence", (execution_id,))
            for rows in bounded_rows(cursor, size=_row_size):
                yield tuple(row[0] for row in rows)

    def current(self, dataset: str) -> RecordKey | None:
        require_text(dataset, "dataset")
        with self._transaction() as connection:
            return connection.execute("SELECT kind,record_id FROM heads WHERE dataset=?", (dataset,)).fetchone()

    def recovery_progress(self, *, exclude: Iterable[RecordKey] = ()) -> Iterator[tuple[tuple[str, bytes], ...]]:
        """Stream the exact progress frontiers understood by resume/recover."""
        with self._transaction() as connection:
            self._requests(connection, exclude, names=("kind", "record_id"), normalize=_key)
            cursor = connection.execute(
                "SELECT 'checkpoint',p.payload FROM progress p JOIN retention t ON t.kind='execution' AND t.record_id=p.execution_id "
                "WHERE t.available=1 AND p.status='incomplete' AND json_type(p.payload,'$.description.checkpoint')='object' "
                "AND p.sequence=(SELECT max(q.sequence) FROM progress q WHERE q.execution_id=p.execution_id) "
                "AND NOT EXISTS(SELECT 1 FROM wanted w WHERE w.kind='execution' AND w.record_id=p.execution_id) "
                "UNION ALL SELECT 'publication',p.payload FROM progress p JOIN retention t ON t.kind='execution' AND t.record_id=p.execution_id "
                "WHERE t.available=1 AND json_type(p.payload,'$.description.publication')='object' "
                "AND p.sequence=(SELECT max(q.sequence) FROM progress q WHERE q.execution_id=p.execution_id AND json_type(q.payload,'$.description.publication')='object') "
                "AND NOT EXISTS(SELECT 1 FROM retention r WHERE r.kind='result' AND r.record_id=p.execution_id||':result') "
                "AND NOT EXISTS(SELECT 1 FROM wanted w WHERE w.kind='execution' AND w.record_id=p.execution_id)"
            )
            for rows in bounded_rows(cursor, size=_row_size):
                yield tuple(rows)

    def select_current(self, update_id: str, dataset: str, target: RecordKey, expected_current: RecordKey | None) -> bool:
        require_text(dataset, "dataset")
        target = _key(target)
        expected_current = None if expected_current is None else _key(expected_current)
        if target[0] not in {"state", "result"}:
            raise IntegrityError("current target must be a retained state or result")
        payload = _encode({"dataset": dataset, "target": list(target), "expected_current": None if expected_current is None else list(expected_current)})
        with self.content_guard(), self._transaction(write=True) as connection:
            if not self._unit(connection, update_id, "current", payload):
                return False
            existing = connection.execute("SELECT kind,record_id FROM heads WHERE dataset=?", (dataset,)).fetchone()
            if existing != expected_current:
                raise StaleBaseError("dataset current target differs from its expected value")
            if connection.execute("SELECT available FROM retention WHERE kind=? AND record_id=?", target).fetchone() != (1,):
                raise IntegrityError("current target is not retained and available")
            connection.execute(
                "INSERT INTO heads VALUES (?,?,?,?) ON CONFLICT(dataset) DO UPDATE SET kind=excluded.kind,record_id=excluded.record_id,update_id=excluded.update_id",
                (dataset, *target, update_id),
            )
            return True

    def retained_records(self, *, kind: str | None = None) -> Iterator[tuple[StoredRecord, ...]]:
        """Stream retained descriptions through the ordinary canonical row reader."""
        if kind is not None:
            require_text(kind, "record kind")
        with self._transaction() as connection:
            cursor = connection.execute("SELECT kind,record_id FROM retention "
                + ("WHERE kind=? " if kind is not None else "") + "ORDER BY kind,record_id",
                () if kind is None else (kind,))
            for keys in bounded_rows(cursor, size=_row_size):
                yield from self.read_records(keys)

    def source_layers(self, *, exclude: Iterable[RecordKey] = (), include: Iterable[RecordKey] | None = None) -> Iterator[tuple[LayerRef, ...]]:
        """Available canonical rows pin their physical source layers."""
        with self._transaction() as connection:
            self._requests(connection, exclude if include is None else include, names=("kind", "record_id"), normalize=_key)
            condition = "NOT EXISTS" if include is None else "EXISTS"
            cursor = connection.execute("SELECT DISTINCT l.reference FROM records r JOIN retention t USING(kind,record_id) JOIN record_layers l ON l.layer_id=r.source_layer WHERE t.available=1 AND " + condition + "(SELECT 1 FROM wanted w WHERE w.kind=r.kind AND w.record_id=r.record_id)")
            for rows in bounded_rows(cursor, size=_row_size):
                yield tuple(LayerRef.from_dict(decode_canonical_json_value(row[0], label="record layer reference")) for row in rows)

    def removal_blockers(self, keys: Iterable[RecordKey]) -> Iterator[tuple[RecordKey, ...]]:
        with self._transaction() as connection:
            self._requests(connection, keys, names=("kind", "record_id"), normalize=_key)
            cursor = connection.execute(
                "SELECT h.kind,h.record_id FROM heads h JOIN wanted w USING(kind,record_id) "
                "UNION SELECT l.owner_kind,l.owner_id FROM links l JOIN wanted w "
                "ON l.target_kind=w.kind AND l.target_id=w.record_id "
                "JOIN retention r ON r.kind=l.owner_kind AND r.record_id=l.owner_id "
                "WHERE r.available=1 AND l.relation IN ('requires','representation') "
                "AND NOT EXISTS(SELECT 1 FROM wanted other WHERE other.kind=l.owner_kind AND other.record_id=l.owner_id)"
            )
            for rows in bounded_rows(cursor, size=_row_size):
                yield tuple(rows)

    def begin_removal(self, update_id: str, policy_id: str, keys: Iterable[RecordKey], *, content: Iterable[RemovalContent] = ()) -> bool:
        require_text(policy_id, "retention policy")
        targets = sorted(set(_key(key) for key in _collect(keys)))
        with self.content_guard(exclusive=True), self._transaction(write=True) as connection:
            connection.execute("CREATE TEMP TABLE planned_content(store TEXT,locator TEXT,reference BLOB,PRIMARY KEY(store,locator)) WITHOUT ROWID")
            with owned_iterator(content) as items:
                for item in items:
                    if item.store not in {"blobs", "records"}:
                        raise IntegrityError("removal content names an unsupported store")
                    encoded = _encode(item.reference.to_dict())
                    previous = connection.execute("SELECT reference FROM planned_content WHERE store=? AND locator=?", (item.store, item.reference.locator)).fetchone()
                    if previous is not None and previous != (encoded,):
                        raise IntegrityError("removal content has conflicting physical references")
                    connection.execute("INSERT OR IGNORE INTO planned_content VALUES (?,?,?)", (item.store, item.reference.locator, encoded))
            digest = hashlib.sha256(_encode({"policy_id": policy_id, "targets": [list(key) for key in targets]}))
            count = 0
            for row in connection.execute("SELECT store,locator,reference FROM planned_content ORDER BY store,locator"):
                digest.update(_encode([row[0], row[1], decode_canonical_json_value(row[2], label="removal reference")]))
                count += 1
            if not targets and not count:
                raise IntegrityError("removal intent requires a retained or physical target")
            if not self._unit(connection, update_id, "removal", digest.digest()):
                return False
            connection.execute("INSERT INTO removals(update_id,policy_id) VALUES (?,?)", (update_id, policy_id))
            connection.executemany("INSERT INTO removal_targets VALUES (?,?,?)", ((update_id, *key) for key in targets))
            connection.execute("INSERT INTO removal_content(update_id,store,locator,reference) SELECT ?,store,locator,reference FROM planned_content", (update_id,))
            connection.execute(
                "UPDATE retention SET available=0,evidence_version=evidence_version+1 WHERE available=1 AND (kind,record_id) IN "
                "(SELECT kind,record_id FROM removal_targets WHERE update_id=?)", (update_id,),
            )
            return True

    def removal_outcomes(self, update_id: str, *, pending_only: bool = False) -> Iterator[tuple[RemovalOutcome, ...]]:
        with self._transaction() as connection:
            cursor = connection.execute(
                "SELECT store,reference,status,error FROM removal_content WHERE update_id=? "
                + ("AND status IN ('pending','failed') " if pending_only else "") + "ORDER BY store,locator", (update_id,))
            for rows in bounded_rows(cursor, size=_row_size):
                yield tuple(RemovalOutcome(RemovalContent(row[0], BlobRef.from_dict(decode_canonical_json_value(row[1], label="removal reference"))), row[2], row[3]) for row in rows)

    def removal(self, update_id: str) -> tuple[str, tuple[RecordKey, ...], bool] | None:
        with self._transaction() as connection:
            row = connection.execute("SELECT policy_id,complete FROM removals WHERE update_id=?", (update_id,)).fetchone()
            if row is None:
                return None
            keys = tuple(connection.execute("SELECT kind,record_id FROM removal_targets WHERE update_id=? ORDER BY kind,record_id", (update_id,)))
            return row[0], keys, bool(row[1])

    def record_removal_outcome(self, update_id: str, outcome: RemovalOutcome) -> None:
        if outcome.status not in {"deleted", "absent", "retained", "failed"}:
            raise IntegrityError("invalid removal outcome")
        if outcome.error is not None:
            require_text(outcome.error, "removal failure")
        with self.content_guard(exclusive=True), self._transaction(write=True) as connection:
            if connection.execute("UPDATE removal_content SET status=?,error=? WHERE update_id=? AND store=? AND locator=? AND reference=?",
                                  (outcome.status, outcome.error, update_id, outcome.content.store, outcome.content.reference.locator, _encode(outcome.content.reference.to_dict()))).rowcount != 1:
                raise IntegrityError("removal outcome has no matching retained intent")

    def finish_removal(self, update_id: str) -> None:
        with self.content_guard(exclusive=True), self._transaction(write=True) as connection:
            if connection.execute("SELECT 1 FROM removal_content WHERE update_id=? AND status IN ('pending','failed') LIMIT 1", (update_id,)).fetchone():
                raise IntegrityError("removal still has unfinished physical targets")
            if connection.execute("UPDATE removals SET complete=1 WHERE update_id=?", (update_id,)).rowcount != 1:
                raise IntegrityError("removal has no retained policy intent")

    def pending_removals(self) -> Iterator[tuple[tuple[str, str, RecordKey | None], ...]]:
        with self._transaction() as connection:
            cursor = connection.execute(
                "SELECT r.update_id,r.policy_id,t.kind,t.record_id FROM removals r LEFT JOIN removal_targets t USING(update_id) "
                "WHERE r.complete=0 ORDER BY r.update_id,t.kind,t.record_id"
            )
            for rows in bounded_rows(cursor, size=_row_size):
                yield tuple((row[0], row[1], None if row[2] is None else (row[2], row[3])) for row in rows)
