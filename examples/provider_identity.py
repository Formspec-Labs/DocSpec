"""Record installed source-provider identity for the executable examples."""

import json
from importlib.metadata import distribution

from docspec.domain.identity import identity_digest, sha256_digest


def provider_installation() -> dict:
    """Pin installed provider bytes even when the installer omits a wheel hash."""
    installed = distribution("spicy-docs")
    direct = json.loads(installed.read_text("direct_url.json") or "{}")
    if direct.get("dir_info", {}).get("editable"):
        raise ValueError("install a SpicyDocs wheel; this example does not support editable provider installs")
    archive = direct.get("archive_info", {})
    wheel_hash = archive.get("hashes", {}).get("sha256")
    files = {str(path): sha256_digest(installed.locate_file(path).read_bytes())
             for path in installed.files or ()
             if str(path).startswith("spicy_docs/") and not str(path).endswith(".pyc")}
    if not files:
        raise ValueError("installed SpicyDocs package has no recorded source files")
    return {"package": "spicy-docs", "version": installed.version,
            "wheelSha256": "sha256:" + wheel_hash if wheel_hash else None,
            "installedFilesSha256": identity_digest(files)}
