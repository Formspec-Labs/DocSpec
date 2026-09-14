"""Iceberg catalog access and contained, relocatable local snapshot files.

DuckDB writes data; PyIceberg owns metadata parsing and REST catalog operations.
The catalog holds disposable write handles. Published states pin immutable metadata.
"""

from dataclasses import dataclass
import os
from pathlib import Path

from pyiceberg.catalog import load_catalog
from pyiceberg.catalog.noop import NoopCatalog
from pyiceberg.exceptions import NamespaceAlreadyExistsError
from pyiceberg.io.pyarrow import PyArrowFileIO
from pyiceberg.table import StaticTable
from pyiceberg.table.metadata import TableMetadataUtil

from docspec.adapters.storage.files import _contained, _read_exact, _sync_file, _sync_parents, _write_once, sha256_file
from docspec.domain.identity import canonical_value_bytes, decode_canonical_json_value
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError


def literal(value):
    return "'" + str(value).replace("'", "''") + "'"


def identifier(value):
    return '"' + value.replace('"', '""') + '"'


@dataclass(frozen=True)
class IcebergCatalog:
    uri: str
    token: str | None = None
    namespace: str = 'docspec'

    @classmethod
    def environment(cls):
        uri = os.environ.get('DOCSPEC_ICEBERG_URI')
        return None if not uri else cls(uri, os.environ.get('DOCSPEC_ICEBERG_TOKEN'))

    def client(self):
        options = {'token': self.token} if self.token else {}
        cat = load_catalog('docspec', type='rest', uri=self.uri, **options)
        try:
            cat.create_namespace(self.namespace)
        except NamespaceAlreadyExistsError:
            pass
        return cat

    def attach(self, connection):
        auth = f'TOKEN {literal(self.token)}' if self.token else "AUTHORIZATION_TYPE 'none'"
        connection.execute(f"ATTACH '' AS iceberg (TYPE iceberg, ENDPOINT {literal(self.uri)}, {auth})")


class SnapshotIO(PyArrowFileIO):
    """Map a table's original absolute paths into its retained local directory."""

    def __init__(self, root, location):
        super().__init__()
        self.root, self.location = root, location.rstrip('/') + '/'

    def path(self, location):
        if not location.startswith(self.location):
            raise IntegrityError('Iceberg file escapes its retained table directory')
        return _contained(self.root, location[len(self.location):])

    def new_input(self, location):
        return super().new_input(str(self.path(location)))


def snapshot(root, reference):
    metadata = BlobRef.from_dict(reference)
    path = _contained(root, metadata.locator)
    if not path.is_file():
        raise IntegrityError('Iceberg metadata is unavailable')
    if sha256_file(path) != (metadata.digest, metadata.byte_size):
        raise IntegrityError('Iceberg metadata differs from its retained reference')
    document = TableMetadataUtil.parse_raw(_read_exact(root, metadata.locator))
    io = SnapshotIO(path.parent.parent, document.location)
    return StaticTable(('retained',), document, str(path), io, NoopCatalog('retained'))


def checksum_reference(root, path):
    receipt = path.relative_to(root).as_posix() + '.sha256'
    digest, size = sha256_file(_contained(root, receipt))
    return BlobRef(receipt, digest, size, 'application/json')


def seal(root, path, children=()):
    """Bind a file and its recovery checksums once, following Iceberg's own tree."""
    locator = path.relative_to(root).as_posix()
    if not _contained(root, locator + '.sha256').exists():
        _sync_file(path)
        _sync_parents(root, path.parent)
        digest, size = sha256_file(path)
        media = 'application/vnd.apache.parquet' if path.suffix == '.parquet' else 'application/octet-stream'
        reference = BlobRef(locator, digest, size, media)
        payload = {'file': reference.to_dict(), 'children': [child.to_dict() for child in children]}
        _write_once(root, locator + '.sha256', canonical_value_bytes(payload))
    return checksum_reference(root, path)


def recovery_references(root, receipt, *, depth=0):
    """Check the pinned checksum tree and enumerate children before parents."""
    if depth > 3:
        raise IntegrityError('Iceberg checksum tree exceeds metadata, list, manifest and data levels')
    payload = _read_exact(root, receipt.locator, max_bytes=32 * 1024**2)
    from docspec.domain.identity import sha256_digest
    if len(payload) != receipt.byte_size or sha256_digest(payload) != receipt.digest:
        raise IntegrityError('Iceberg recovery checksum differs from its retained reference')
    try:
        value = decode_canonical_json_value(payload, label='Iceberg recovery checksum')
        if set(value) != {'file', 'children'}:
            raise ValueError('invalid checksum shape')
        reference = BlobRef.from_dict(value['file'])
        if receipt.locator != reference.locator + '.sha256' or not reference.locator.startswith('iceberg/'):
            raise ValueError('checksum names another file')
        children = [BlobRef.from_dict(child) for child in value['children']]
    except (KeyError, TypeError, ValueError) as error:
        raise IntegrityError('invalid Iceberg recovery checksum') from error
    for child in children:
        yield from recovery_references(root, child, depth=depth + 1)
    yield reference
    yield receipt


def snapshot_files(table):
    """Enumerate exactly this snapshot's recovery files; history has its own pins."""
    current = table.current_snapshot()
    if current is not None:
        try:
            for manifest in current.manifests(table.io):
                for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True):
                    yield table.io.path(entry.data_file.file_path)
                yield table.io.path(manifest.manifest_path)
            yield table.io.path(current.manifest_list)
        except OSError as error:
            raise IntegrityError('Iceberg recovery metadata is unavailable') from error
    yield Path(table.metadata_location)


def seal_snapshot(root, table):
    """Share existing manifest checksums; seal new files before publishing a pin."""
    current = table.current_snapshot()
    lists = []
    if current is not None:
        manifests = []
        for manifest in current.manifests(table.io):
            path = table.io.path(manifest.manifest_path)
            if _contained(root, path.relative_to(root).as_posix() + '.sha256').exists():
                receipt = checksum_reference(root, path)
            else:
                children = (seal(root, table.io.path(entry.data_file.file_path))
                            for entry in manifest.fetch_manifest_entry(table.io, discard_deleted=True))
                receipt = seal(root, path, children)
            manifests.append(receipt)
        lists.append(seal(root, table.io.path(current.manifest_list), manifests))
    return seal(root, Path(table.metadata_location), lists)
