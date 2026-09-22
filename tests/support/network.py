"""Fixture processing may use the configured local storage catalog only."""

import os
import socket
from urllib.parse import urlsplit


def storage_only(connect):
    """Return a connect wrapper that permits only the configured storage catalog host."""
    catalog = urlsplit(os.environ.get("DOCSPEC_ICEBERG_URI", ""))
    allowed = {(item[4][0], catalog.port or 80) for item in socket.getaddrinfo(
        catalog.hostname, catalog.port or 80, type=socket.SOCK_STREAM)} if catalog.hostname else set()
    if catalog.hostname:
        allowed.add((catalog.hostname, catalog.port or 80))

    def checked(*args, **kwargs):
        address = args[1] if isinstance(args[0], socket.socket) else args[0]
        if not isinstance(address, tuple) or address[:2] not in allowed:
            raise AssertionError("fixture processing attempted a connection outside its storage catalog")
        return connect(*args, **kwargs)

    return checked
