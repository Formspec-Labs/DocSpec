"""The Iceberg write catalog each record store runs for itself.

DuckDB's Iceberg extension attaches only REST catalogs, so this module answers
exactly the REST calls DuckDB makes, and PyIceberg's SQL catalog applies every
requirement and update. Nothing reads a catalog back: published states pin
metadata files by path. Record storage imports this module only to write.
"""

from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import os
from pathlib import Path
import secrets
import selectors
from threading import Thread
from types import SimpleNamespace
from urllib.parse import unquote, urlsplit

from pyiceberg.catalog.rest import CreateTableRequest, TableResponse
from pyiceberg.catalog.sql import SqlCatalog
from pyiceberg.exceptions import (CommitFailedException, NoSuchNamespaceError, NoSuchTableError,
                                  TableAlreadyExistsError)
from pyiceberg.partitioning import UNPARTITIONED_PARTITION_SPEC
from pyiceberg.table import CommitTableRequest
from pyiceberg.table.metadata import new_table_metadata
from pyiceberg.table.sorting import UNSORTED_SORT_ORDER
from pyiceberg.typedef import IcebergBaseModel
from sqlalchemy.engine import URL

from docspec.adapters.storage.iceberg import literal


class InProcessCatalog:
    """PyIceberg's SQL catalog in this process, served to DuckDB over loopback REST.

    Its SQLite file lives in the writer's scratch directory: every handle is
    registered, committed and dropped within one write, so none may outlive the
    connection that attached it. A random bearer token admits only that connection.
    """

    namespace = 'docspec'

    def __init__(self, directory):
        # A URL object, not a string: a "?" in the directory would start a URL query.
        path = Path(directory) / 'iceberg-catalog.sqlite'
        self._catalog = SqlCatalog('docspec', uri=URL.create('sqlite', database=str(path)))
        self._catalog.create_namespace(self.namespace)
        self._server = ThreadingHTTPServer(('127.0.0.1', 0), _Handler)
        self._server.catalog, self._server.token = self._catalog, secrets.token_urlsafe(32)
        self._wake = os.pipe()
        self._thread = Thread(target=self._serve, daemon=True, name='docspec-iceberg-catalog')
        self._thread.start()

    def _serve(self):
        # Block until a request or close(); serve_forever would poll, and its interval would delay every close.
        with selectors.DefaultSelector() as selector:
            selector.register(self._server, selectors.EVENT_READ)
            selector.register(self._wake[0], selectors.EVENT_READ)
            while all(key.fd != self._wake[0] for key, _ in selector.select()):
                self._server.handle_request()

    def client(self):
        """Return the SQL catalog itself; PyIceberg's own calls need no HTTP."""

        return self._catalog

    def attach(self, connection):
        """Attach DuckDB to the loopback adapter with this catalog's token."""

        endpoint, token = f'http://127.0.0.1:{self._server.server_port}', self._server.token
        connection.execute(f"ATTACH '' AS iceberg (TYPE iceberg, ENDPOINT {literal(endpoint)}, TOKEN {literal(token)})")

    def close(self):
        """Stop serving and release the SQLite engine."""

        os.write(self._wake[1], b'\0')
        self._thread.join()
        for descriptor in self._wake:
            os.close(descriptor)
        self._server.server_close()
        self._catalog.engine.dispose()


# First match wins: request validation errors are ValueErrors.
_ERRORS = (
    (PermissionError, 401, 'NotAuthorizedException'),
    (NoSuchTableError, 404, 'NoSuchTableException'),
    (NoSuchNamespaceError, 404, 'NoSuchNamespaceException'),
    (CommitFailedException, 409, 'CommitFailedException'),
    (TableAlreadyExistsError, 409, 'AlreadyExistsException'),
    (ValueError, 400, 'BadRequestException'),
)


class _Handler(BaseHTTPRequestHandler):
    """Serve the server's catalog to the bearer of its token."""

    def log_message(self, format, *args):
        """Stay quiet: DuckDB reports a refused call through the write that made it."""

    def _serve(self):
        try:
            presented = self.headers.get('Authorization', '').encode()
            if not secrets.compare_digest(presented, b'Bearer ' + self.server.token.encode()):
                raise PermissionError('missing or invalid catalog token')
            size = int(self.headers.get('Content-Length') or 0)
            body = json.loads(self.rfile.read(size)) if size else {}
            path = [unquote(part) for part in urlsplit(self.path).path.split('/')[1:]]
            status, payload = _route(self.server.catalog, self.command, path, body)
        except Exception as error:
            status, kind = next(((code, kind) for kinds, code, kind in _ERRORS if isinstance(error, kinds)),
                                (500, 'InternalServerError'))
            payload = {'error': {'message': str(error), 'type': kind, 'code': status}}
        data = b'' if payload is None else (
            payload.model_dump_json() if isinstance(payload, IcebergBaseModel) else json.dumps(payload)).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = _serve


def _route(catalog, method, path, body):
    """Answer one call DuckDB makes: configuration, listings, loads, staged creation and commits."""

    parts = path
    if path[1:2] == ['namespaces'] and len(path) > 2:  # A namespace segment separates its levels with 0x1F.
        parts = [*path[:2], tuple(path[2].split('\x1f')), *path[3:]]
    match method, parts:
        case 'GET', ['v1', 'config']:
            return 200, {'defaults': {}, 'overrides': {}}
        case 'GET', ['v1', 'namespaces']:  # Catalog enumeration, as SHOW TABLES performs.
            return 200, {'namespaces': [list(name) for name in catalog.list_namespaces()]}
        case 'GET', ['v1', 'namespaces', namespace, 'tables']:
            names = catalog.list_tables(namespace)
            return 200, {'identifiers': [{'namespace': list(name[:-1]), 'name': name[-1]} for name in names]}
        case 'GET', ['v1', 'namespaces', namespace]:
            return 200, {'namespace': list(namespace), 'properties': catalog.load_namespace_properties(namespace)}
        case 'GET', ['v1', 'namespaces', namespace, 'tables', table]:
            loaded = catalog.load_table((*namespace, table))
            return 200, TableResponse(metadata_location=loaded.metadata_location, metadata=loaded.metadata, config={})
        case 'POST', ['v1', 'namespaces', namespace, 'tables']:
            request = CreateTableRequest.model_validate(body)
            # DuckDB stages creation in its transaction and commits it later with assert-create.
            if not request.stage_create:
                raise ValueError('the in-process catalog creates only staged tables')
            metadata = new_table_metadata(schema=request.table_schema, location=request.location,
                                          partition_spec=request.partition_spec or UNPARTITIONED_PARTITION_SPEC,
                                          sort_order=request.write_order or UNSORTED_SORT_ORDER,
                                          properties=dict(request.properties))
            return 200, TableResponse(metadata=metadata, config={})
        case 'POST', ['v1', 'namespaces', namespace, 'tables', table]:
            return 200, _commit(catalog, {**body, 'identifier': {'namespace': list(namespace), 'name': table}})
        case 'POST', ['v1', 'transactions', 'commit']:
            changes = body.get('table-changes', [])
            if len(changes) != 1:
                raise ValueError('the in-process catalog commits exactly one table per transaction')
            _commit(catalog, changes[0])
            return 204, None
    raise ValueError(f'unsupported Iceberg REST call: {method} /{"/".join(path)}')


def _commit(catalog, change):
    """Check one table's requirements and apply its updates; a missing table is a staged creation."""

    request = CommitTableRequest.model_validate(change)
    identifier = (*request.identifier.namespace.root, request.identifier.name)
    # SqlCatalog.commit_table reads only the handle's name, then loads the current table itself.
    return catalog.commit_table(SimpleNamespace(name=lambda: identifier), request.requirements, request.updates)
