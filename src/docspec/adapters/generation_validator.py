"""Local implementation identity for reuse of DocSpec's own completed admission."""

import hashlib
import platform
from functools import cache
from importlib import import_module
from importlib.metadata import version
from pathlib import Path

from docspec.domain.identity import canonical_value_bytes, sha256_digest
from docspec.errors import IntegrityError


@cache
def _owner_sources():
    """Sorted (path, sha256) of every Python file in the owner packages, hashed once per process.

    Modules are imported once, so re-hashing the files on disk per call would not
    bind the code that is running; this digest binds what was loaded, taken at
    first use. Exceptions are not cached: a bad tree refuses on every call.
    """
    packages = {}
    for name in ("docspec", "spicy_docs", "rulespec_artifacts"):
        root = Path(import_module(name).__file__).parent
        paths = sorted(root.rglob("*.py"))
        if root.is_symlink() or not paths or any(path.is_symlink() for path in root.rglob("*")):
            raise IntegrityError("generation validator requires complete nonsymlink owner source trees")
        packages[name] = [(path.relative_to(root).as_posix(), hashlib.sha256(path.read_bytes()).hexdigest())
                          for path in paths]
    return packages


def validator_identity(table=None):
    """Bind owner source bytes (hashed once per process) and native runtimes, never producer claims.

    This deliberately covers the complete owner Python packages so a changed
    helper cannot silently preserve a skipped semantic verdict. The table policy
    and package versions are read on every call.
    """
    from docspec.adapters.generation_source import TABLE_CONTRACTS

    contract = TABLE_CONTRACTS.get(table)
    policy = None if contract is None else {
        "columns": list(getattr(contract, "columns", ())), "identity": list(contract.identity),
        "types": dict(getattr(contract, "types", {})), "keySpelling": getattr(contract, "key_spelling", None),
    }
    return sha256_digest(canonical_value_bytes({
        "sources": _owner_sources(),
        "tablePolicy": policy,
        "python": [platform.python_implementation(), platform.python_version()],
        "versions": {name: version(name) for name in
                     ("docspec", "spicy-docs", "rulespec-artifacts", "duckdb", "pyarrow", "pyiceberg")},
    }))
