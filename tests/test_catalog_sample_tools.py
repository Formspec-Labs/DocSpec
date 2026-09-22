"""Retained sampling frames keep their source values, order, and distinct uses."""

from __future__ import annotations

import gzip
import hashlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

from tools import (
    description_coverage,
    description_prevalence,
    draw_docabstract_companion,
    draw_docabstract_sample,
    fr_topic_receipt,
    select_attachment_sample,
)
from tools.catalog_sample_support import (
    iter_source_item_rows,
    report_catalog_root,
    source_item_member_paths,
)


def _catalog(tmp_path: Path) -> tuple[Path, Path]:
    """Write a tiny catalog whose two source-item members carry regulations.gov and Federal Register rows."""
    root, blobs = tmp_path / "catalog", tmp_path / "blobs"
    (root / "manifests").mkdir(parents=True)
    blobs.mkdir()
    document_rows = []
    for index, abstract in enumerate(("Federal Register of 2026, 91 FR 1", None, " \t", "absent")):
        attributes = {
            "agencyId": "EPA", "documentType": "Rule", "postedDate": "2026-01-01",
            "title": "Example", "docketId": "EPA-D", "docAbstract": abstract,
            "fileFormats": [{"format": "pdf", "fileUrl": "https://example.invalid/body.pdf", "size": 10}],
        }
        if abstract == "absent":
            del attributes["docAbstract"]
        document_rows.append({
            "documentId": f"EPA-{index}",
            "selection": {"disposition": "selected" if index == 1 else "unavailable"},
            "sourceNativeFacts": [
                {"scopeId": "regulations-gov-documents", "fields": {"data": {"attributes": attributes}}},
                {"scopeId": "regulations-gov-dockets", "fields": {"data": {"attributes": {
                    "dkAbstract": "  Subject: Example\n", "title": "Example",
                }}}},
            ],
        })
    topics = [{"label": "Air", "observedTopicScheme": "federalregister.gov", "observedTopicId": "Air"}]
    fr_rows = [
        {
            "documentId": f"FR-{index}", "sourceObservedTopics": observed,
            "sourceNativeFacts": [{"scopeId": "federal-register-documents", "fields": {
                "document_number": str(index), "publication_date": published,
                "topics": [t["label"] for t in observed], "abstract": "  Verbatim\ntext.  ",
            }}],
        }
        for index, (observed, published) in enumerate(((topics, "2026-01-01"), ([], "2026-01-01"), ([], "1999-01-01")))
    ]
    (blobs / "a").write_bytes(gzip.compress(b"\n" + b"\n \t\n".join(json.dumps(row).encode() for row in document_rows)))
    (blobs / "z").write_bytes(b"\n".join(json.dumps(row).encode() for row in fr_rows))
    (root / "manifests" / "catalog.json").write_text(json.dumps({"members": [
        {"role": "source-items", "blobRef": "sha256:z"},
        {"role": "unread-role", "blobRef": "sha256:missing"},
        {"role": "source-items", "blobRef": "sha256:a"},
    ]}))
    return root, blobs


def _receipt(path: Path) -> dict:
    """Load a receipt only after its `.sha256` sidecar matches the file bytes."""
    payload = path.read_bytes()
    assert path.with_name(path.name + ".sha256").read_text().split()[0] == hashlib.sha256(payload).hexdigest()
    return json.loads(payload)


def test_detached_members_keep_blob_order_blank_lines_and_exact_values(tmp_path: Path) -> None:
    root, blobs = _catalog(tmp_path)
    assert source_item_member_paths(root, blobs) == [str(blobs / "a"), str(blobs / "z")]
    regs = list(iter_source_item_rows(str(blobs / "a")))
    assert [row["documentId"] for row in regs] == ["EPA-0", "EPA-1", "EPA-2", "EPA-3"]
    assert regs[2]["sourceNativeFacts"][0]["fields"]["data"]["attributes"]["docAbstract"] == " \t"
    assert list(iter_source_item_rows(str(blobs / "z")))[0]["documentId"] == "FR-0"
    (blobs / "broken").write_bytes(b'{"documentId": "ok"}\nnot-json')
    with pytest.raises(json.JSONDecodeError):
        list(iter_source_item_rows(str(blobs / "broken")))


def test_catalog_display_paths_preserve_home_relative_and_other_paths(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "home"))
    assert report_catalog_root(tmp_path / "home" / "catalog") == "~/catalog"
    assert report_catalog_root(tmp_path / "catalog") == str(tmp_path / "catalog")
    assert report_catalog_root(Path("relative/catalog")) == "relative/catalog"


def test_research_consumers_keep_distinct_predicates_and_outside_home_receipts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root, blobs = _catalog(tmp_path)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path / "another-home"))
    common = ["--catalog-root", str(root), "--blob-store", str(blobs), "--workers", "1"]
    # Real process workers exercise importability and the shared reader on both
    # member encodings. Only the publisher call is replaced; no network is used.
    coverage = description_coverage.measure(root, blobs, "regulations-gov-documents", 1)
    assert (coverage["rows"], coverage["present"]) == (4, 1)
    assert coverage["emptyBreakdown"] == {"null": 1, "absent": 1, "blankString": 1}
    fr_coverage = description_coverage.measure(root, blobs, "federal-register-documents", 1)
    assert (fr_coverage["rows"], fr_coverage["present"]) == (3, 3)

    prevalence_path = tmp_path / "prevalence.json"
    assert description_prevalence.main([*common, "--out", str(prevalence_path)]) == 0
    prevalence = _receipt(prevalence_path)
    assert prevalence["catalog"] == str(root)
    assert prevalence["predicates"]["1"]["count"] == 1
    assert prevalence["predicates"]["3"]["byDocket"]["subjectEqualsTitle"] == 1
    assert prevalence["predicates"]["3"]["byDocumentInheriting"]["subjectEqualsTitle"] == 4

    sample_path = tmp_path / "sample.json"
    assert draw_docabstract_sample.main([*common, "--out", str(sample_path), "--per-stratum", "1"]) == 0
    sample = _receipt(sample_path)
    assert sample["catalog"] == str(root)
    assert [row["documentId"] for row in sample["rows"]] == ["EPA-0", "EPA-0"]
    assert {row["stratum"] for row in sample["rows"]} == {"populated-rule-or-proposed", "populated-2016-or-later"}
    original = sample_path.read_bytes()
    companion_path = tmp_path / "companion.json"
    assert draw_docabstract_companion.main([*common, "--first-receipt", str(sample_path), "--out", str(companion_path)]) == 0
    companion = _receipt(companion_path)
    assert companion["catalog"] == str(root)
    assert companion["documentsRequested"] == companion["documentsFound"] == 1
    assert companion["rows"][0]["docketAbstract"] == "  Subject: Example\n"
    assert companion["joinsTo"]["sha256"] == hashlib.sha256(original).hexdigest()
    assert sample_path.read_bytes() == original

    selection = select_attachment_sample.build_selection(
        catalog_root=root, blob_store=blobs, salt="test", allocation={("(none)", "minor"): 3}, workers=1,
    )
    assert selection["frame"]["catalogRoot"] == str(root)
    assert {row["documentId"] for row in selection["rows"]} == {"EPA-0", "EPA-2", "EPA-3"}
    assert {row["partitionId"] for row in selection["rows"]} == {"00"}

    monkeypatch.setattr(fr_topic_receipt, "fetch_live", lambda number, timeout: {
        "status": 200, "topicsKeyPresent": True, "liveTopics": ["Air"] if number == "0" else [],
    })
    topic_path = tmp_path / "topics.json"
    assert fr_topic_receipt.main([*common, "--out", str(topic_path), "--per-stratum", "1", "--delay", "0"]) == 0
    topic = _receipt(topic_path)
    assert topic["catalog"]["root"] == str(root)
    assert (topic["agree"], topic["disagree"]) == (3, 0)
    assert [row["stratum"] for row in topic["rows"]] == ["populated", "empty-post-2000", "empty-pre-2000"]


@pytest.mark.parametrize("name", [
    "description_coverage", "description_prevalence", "draw_docabstract_sample",
    "draw_docabstract_companion", "select_attachment_sample", "fr_topic_receipt",
])
def test_research_tool_direct_script_entry_points_still_import(name: str, tmp_path: Path) -> None:
    script = Path(__file__).resolve().parents[1] / "tools" / f"{name}.py"
    completed = subprocess.run([sys.executable, str(script), "--help"], cwd=tmp_path, capture_output=True, text=True)
    assert completed.returncode == 0, completed.stderr
    assert "usage:" in completed.stdout
