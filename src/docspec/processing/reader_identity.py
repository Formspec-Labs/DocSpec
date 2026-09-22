"""Pin selected installed SpicyDocs source files when configuring a stage.

These hashes identify installed files, not loaded-code or environment attestation.
Reading distribution metadata does not import any optional parser backend.
"""

from hashlib import sha256
from importlib.metadata import PackageNotFoundError, distribution

from docspec.errors import IntegrityError

ReaderIdentity = tuple[str, tuple[tuple[str, str], ...]]
MARKUP_MODULES = ("spicy_docs.reading.markup", "spicy_docs.reading.xml")
IMAGE_MODULES = ("spicy_docs.reading.image_header",)
JSON_MODULES = ("spicy_docs.reading.json_input",)
PDF_MODULES = ("spicy_docs.extraction.pypdf",)


def installed_reader_identity(modules: tuple[str, ...]) -> ReaderIdentity | None:
    """Return the installed distribution version and module digests, or None when unavailable."""

    try:
        owner = distribution("spicy-docs")
        hashes = tuple(
            (name, sha256(owner.locate_file(name.replace(".", "/") + ".py").read_bytes()).hexdigest())
            for name in modules
        )
    except (PackageNotFoundError, OSError):
        return None
    return owner.version, hashes


def reader_configuration(identity: ReaderIdentity | None) -> dict:
    """Shape a reader identity as the configuration fields extractors include in digests."""

    return {
        "readerVersion": identity[0] if identity else None,
        "readerModules": dict(identity[1]) if identity else None,
    }


def require_reader_identity(identity: ReaderIdentity | None, modules: tuple[str, ...]) -> None:
    """Refuse an unavailable configured reader or installed module bytes that changed since configuration."""

    if identity is None:
        raise IntegrityError("the configured SpicyDocs reader files are unavailable")
    if installed_reader_identity(modules) != identity:
        raise IntegrityError("installed SpicyDocs reader differs from the configured version or module bytes")
