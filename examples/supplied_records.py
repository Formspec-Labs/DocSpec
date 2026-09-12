"""Build and inspect supplied records without fetching documents or importing a provider.

Run: python -m examples.supplied_records --output /absolute/new-directory
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from rulespec_artifacts import Producer

from docspec.domain.identity import sha256_digest
from docspec.runtime import build_local_catalog, open_local_catalog
from docspec.source_catalog import SourceCatalogCandidate, SuppliedRecordCatalogPolicy, SuppliedRecordSource
from docspec.workspace import LocalWorkspace


def demonstrate(output: Path) -> dict[str, object]:
    if not output.is_absolute() or output.exists():
        raise ValueError("output must be an absolute path that does not exist")
    namespace = "urn:example:contributor-notes"
    source = SuppliedRecordSource((
        {"recordId": "draft", "sourceIssuedVersion": "draft-3", "title": "Local contribution notes",
         "metadata": {"owner": "contributors", "status": "supplied draft"},
         "candidateRenditions": [SourceCatalogCandidate(
             "body", "text/plain", "immutable-object", "notes.txt",
         ).to_dict()]},
        {"recordId": "metadata-only", "sourceIssuedVersion": "draft-1", "title": "A document to find later",
         "metadata": {"owner": "contributors"}, "candidateRenditions": []},
    ), source_system_id=namespace, source_system_version="1", source_state_scope="complete-snapshot",
       max_records=10, max_bytes=1024**2)
    implementation = "urn:docspec:example:supplied-records:" + sha256_digest(Path(__file__).read_bytes())
    producer = Producer("docspec", implementation, "urn:docspec:verifier:source-catalog", "1.0.0", implementation)
    workspace = LocalWorkspace(output)
    result = build_local_catalog((source,), workspace,
        policy=SuppliedRecordCatalogPolicy(namespace, "1"), catalog_id="urn:example:contributor-notes:catalog",
        producer=producer, max_scratch_bytes=8 * 1024**2)
    catalog = open_local_catalog(result.reference, workspace, producer=producer)
    return {
        "catalog": result.reference.to_dict(),
        "itemCount": catalog.summary.item_count,
        "items": [{"documentId": row["documentId"], "title": row["normalizedMetadata"]["title"],
                   "selection": row["selection"]} for row in catalog.iter_mappings()],
        "evidenceScope": "caller-supplied records; documents have not been fetched",
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(json.dumps(demonstrate(args.output), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
