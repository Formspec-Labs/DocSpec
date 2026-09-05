"""The attachment-sample instruments must fail loudly, not quietly.

Both tools have one failure mode that would be invisible: a selection edited
after sealing, and a response shape this parser guesses wrong. Each would
produce a well-formed, confidently wrong measurement rather than an error, and
each would push the campaign decision in the direction of "do not bother".
These tests pin the two guards that make them visible.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.fetch_attachment_sample import _completed_ids, _scrub, _summarize, read_key  # noqa: E402
from tools.select_attachment_sample import _largest_remainder, _rank  # noqa: E402


def test_rank_is_stable_and_salt_sensitive() -> None:
    assert _rank("s", "EPA-1") == _rank("s", "EPA-1")
    assert _rank("s", "EPA-1") != _rank("t", "EPA-1")
    # The NUL separator keeps salt and id from running together, so no pair of
    # (salt, id) can collide with a different pair by concatenation.
    assert _rank("ab", "c") != _rank("a", "bc")


def test_largest_remainder_hits_the_total_and_keeps_every_key() -> None:
    quota = _largest_remainder(200, {"EPA": 1000, "FDA": 150, "NRC": 29})
    assert sum(quota.values()) == 200
    assert set(quota) == {"EPA", "FDA", "NRC"}
    assert min(quota.values()) >= 1


def test_largest_remainder_drops_only_when_it_must() -> None:
    quota = _largest_remainder(2, {"EPA": 100, "FDA": 50, "NRC": 1})
    assert sum(quota.values()) == 2
    assert set(quota) == {"EPA", "FDA"}


def test_summarize_reads_attachments_and_sizes() -> None:
    payload = {
        "data": {
            "attributes": {"comment": "hello", "restrictReasonType": None, "fileFormats": None},
            "relationships": {"attachments": {"data": [{"id": "a"}, {"id": "b"}]}},
        },
        "included": [
            {
                "type": "attachments",
                "attributes": {
                    "fileFormats": [
                        {"fileUrl": "https://x/1.pdf", "format": "pdf", "size": 10},
                        {"fileUrl": "https://x/2.pdf", "format": "pdf", "size": 5},
                    ]
                },
            }
        ],
    }
    out = _summarize(payload)
    assert out["attachmentCount"] == 2
    assert out["declaredBytes"] == 15
    assert out["inlineCommentChars"] == 5
    assert out["linkedAttachmentCount"] == 2
    assert out["declaredSizesMissing"] == 0


def test_a_genuinely_bare_document_is_distinguishable_from_a_parser_miss() -> None:
    """Zero attachments must not look the same in both cases.

    This is the whole point of the shape signals: a corpus-wide zero is the
    reading that cancels the campaign, so it has to be provable rather than
    merely observed.
    """
    bare = _summarize({"data": {"attributes": {"comment": None}}})
    assert bare["attachmentCount"] == 0
    assert bare["hasIncludedKey"] is False
    assert bare["includedTypes"] == []

    # Same count, but the response plainly carried included entries this parser
    # did not recognise. An analysis that ignores these fields would read the
    # two rows as the same fact.
    missed = _summarize(
        {
            "data": {"attributes": {"comment": None}},
            "included": [{"type": "attachmentDocuments", "attributes": {"files": []}}],
        }
    )
    assert missed["attachmentCount"] == 0
    assert missed["hasIncludedKey"] is True
    assert missed["includedTypes"] == ["attachmentDocuments"]


def test_missing_declared_size_is_counted_not_silently_zero() -> None:
    out = _summarize(
        {
            "data": {"attributes": {}},
            "included": [
                {
                    "type": "attachments",
                    "attributes": {"fileFormats": [{"fileUrl": "u", "format": "pdf", "size": None}]},
                }
            ],
        }
    )
    assert out["attachmentCount"] == 1
    assert out["declaredBytes"] == 0
    assert out["declaredSizesMissing"] == 1


def test_scrub_removes_the_key_from_recorded_text() -> None:
    assert _scrub("failed with key=SECRET123 at host", "SECRET123") == "failed with key=<redacted> at host"


def test_read_key_finds_the_named_key_and_refuses_when_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nOTHER=x\nREGULATIONS_GOV_API_KEY='abc123'\nZYTE_TOKEN=y\n")
    assert read_key(env) == "abc123"

    empty = tmp_path / "empty.env"
    empty.write_text("ZYTE_TOKEN=y\n")
    with pytest.raises(SystemExit):
        read_key(empty)


def test_resume_survives_a_torn_final_line(tmp_path: Path) -> None:
    receipt = tmp_path / "r.ndjson"
    receipt.write_text(
        json.dumps({"documentId": "A-1"}) + "\n" + json.dumps({"documentId": "A-2"}) + "\n" + '{"docume'
    )
    assert _completed_ids(receipt) == {"A-1", "A-2"}


def test_the_sealed_selection_matches_its_sidecar() -> None:
    """The committed selection is the artifact the fetch will refuse to differ from."""
    root = Path(__file__).resolve().parents[1]
    selection = root / "docs" / "history" / "2026-09-05-attachment-sample-selection.json"
    sidecar = selection.with_name(selection.name + ".sha256")
    assert hashlib.sha256(selection.read_bytes()).hexdigest() == sidecar.read_text().split()[0]

    payload = json.loads(selection.read_text())
    assert payload["sampleSize"] == len(payload["rows"]) == 2000
    assert sum(s["population"] for s in payload["strata"]) == 865_206
    assert len({row["documentId"] for row in payload["rows"]}) == 2000
    # Disproportionate allocation is the design, so the weights must not be
    # uniform; reading a raw sample fraction off this set would be wrong.
    assert len({row["designWeight"] for row in payload["rows"]}) == len(payload["strata"])
