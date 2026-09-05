"""The attachment-sample instruments must fail loudly, not quietly.

Both tools have one failure mode that would be invisible: a selection edited
after sealing, and a response shape this parser guesses wrong. Each would
produce a well-formed, confidently wrong measurement rather than an error, and
each would push the campaign decision in the direction of "do not bother".
These tests pin the two guards that make them visible.
"""

from __future__ import annotations

import email.message
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tools.fetch_attachment_sample import (  # noqa: E402
    BROWSER_UA,
    MAGIC,
    _completed_ids,
    api_quota_lock,
    _head,
    _scrub,
    _summarize,
    probe_direct,
    read_key,
)
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
    # One attachment published in two formats: one attachment, two renditions.
    assert out["attachmentCount"] == 1
    assert out["renditionCount"] == 2
    assert out["declaredBytesAllFormats"] == 15
    assert out["declaredBytesFirstFormatPerAttachment"] == 10
    assert out["inlineCommentChars"] == 5
    assert out["linkedAttachmentCount"] == 2
    assert out["declaredSizesMissing"] == 0


def test_an_attachment_carrying_no_file_is_not_the_same_as_no_attachment() -> None:
    """Observed live on FDA-1987-N-0054-0051, 2026-09-05.

    The attachment is in the linkage and in ``included`` and has no
    ``fileFormats`` at all. Counting only downloadable formats would report this
    document identically to one with no attachment, and the campaign's yield
    question needs them apart: one is withheld content, the other is nothing.
    """
    out = _summarize(
        {
            "data": {
                "attributes": {"restrictReasonType": "Confidential Business Information"},
                "relationships": {"attachments": {"data": [{"id": "a"}]}},
            },
            "included": [{"type": "attachments", "attributes": {"fileFormats": []}}],
        }
    )
    assert out["attachmentCount"] == 1
    assert out["renditionCount"] == 0
    assert out["attachmentsWithNoFormats"] == 1
    assert out["linkedAttachmentCount"] == 1
    assert out["declaredBytesAllFormats"] == 0


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
    assert out["renditionCount"] == 1
    assert out["declaredBytesAllFormats"] == 0
    assert out["declaredSizesMissing"] == 1


def test_scrub_removes_the_key_from_recorded_text() -> None:
    assert _scrub("failed with key=SECRET123 at host", "SECRET123") == "failed with key=<redacted> at host"


def test_read_key_finds_the_named_key_and_refuses_when_absent(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text("# comment\nOTHER=x\nAPI_GOV='abc123'\nZYTE_TOKEN=y\n")
    assert read_key(env) == "abc123"
    assert read_key(env, "ZYTE_TOKEN") == "y"

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


def test_the_hosts_two_403s_are_classified_apart(monkeypatch: pytest.MonkeyPatch) -> None:
    """A rejected client and an absent file both answer 403. They are not the same fact.

    Verified live on 2026-09-05: with Python's default User-Agent every URL --
    including files the API had just declared -- returns 403 with a 919-byte
    text/html page, while a genuinely absent key (a nonsense document id) returns
    403 with application/xml and no length. Collapsing the two would turn client
    rejection into evidence that the corpus has no files.
    """

    def reply(content_type: str, length: str | None):
        message = email.message.Message()
        message["Content-Type"] = content_type
        if length is not None:
            message["Content-Length"] = length

        def _open(request, timeout=None):  # noqa: ARG001
            raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", message, None)

        return _open

    monkeypatch.setattr(urllib.request, "urlopen", reply("text/html", "919"))
    assert _head("https://x/a.pdf", 5)["verdict"] == "client-rejected"

    monkeypatch.setattr(urllib.request, "urlopen", reply("application/xml", None))
    assert _head("https://x/a.pdf", 5)["verdict"] == "absent"


def test_magic_bytes_reject_an_error_page_that_arrived_with_a_pdf_name() -> None:
    """A 919-byte HTML error page saved as .pdf must not count as a recovered file."""
    assert any(b"%PDF".startswith(m) or m == b"%PDF" for m in MAGIC["pdf"])
    assert MAGIC["docx"] == MAGIC["xlsx"] == (b"PK\x03\x04",)
    assert b"<html><body>Forbidden"[:4] not in MAGIC["pdf"]


def test_an_unusual_extension_is_fetched_not_guessed_at(monkeypatch: pytest.MonkeyPatch) -> None:
    """The API declares each row's URLs a second earlier, so the grid never has to guess.

    A .wpd file is outside the five-extension grid entirely. Probing the declared
    URL turns it from a false absence into a confirmed hit; without that the
    document would have read as having no content at all.
    """
    served = {
        "https://downloads.regulations.gov/X-1/attachment_1.wpd": 4096,
        "https://downloads.regulations.gov/X-1/attachment_2.pdf": 200,
    }

    def fake_head(url: str, timeout: float) -> dict[str, object]:  # noqa: ARG001
        if url in served:
            return {"url": url, "verdict": "hit", "bytes": served[url], "status": 200}
        return {"url": url, "verdict": "absent", "status": 403}

    monkeypatch.setattr("tools.fetch_attachment_sample._head", fake_head)
    out = probe_direct(
        "X-1",
        ["https://downloads.regulations.gov/X-1/attachment_1.wpd"],
        timeout=5,
        max_index=3,
        hard_cap=25,
        workers=2,
    )
    assert out["declaredCount"] == 1
    assert out["declaredConfirmedCount"] == 1
    assert out["directHitCount"] == 2
    assert out["declaredNotServed"] == []
    # The grid found a second file the API never mentioned: a counted
    # disagreement, which is what makes declared-metadata cost sizing checkable.
    assert out["servedNotDeclared"] == ["https://downloads.regulations.gov/X-1/attachment_2.pdf"]
    assert out["directAgreesWithApi"] is False
    assert out["userAgent"] == BROWSER_UA


def test_declared_but_not_served_is_recorded_separately(monkeypatch: pytest.MonkeyPatch) -> None:
    """Over-counting and under-counting the campaign's bytes are different defects."""
    monkeypatch.setattr(
        "tools.fetch_attachment_sample._head",
        lambda url, timeout: {"url": url, "verdict": "absent", "status": 403},  # noqa: ARG005
    )
    out = probe_direct(
        "X-2",
        ["https://downloads.regulations.gov/X-2/attachment_1.pdf"],
        timeout=5,
        max_index=1,
        hard_cap=25,
        workers=2,
    )
    assert out["declaredNotServed"] == ["https://downloads.regulations.gov/X-2/attachment_1.pdf"]
    assert out["servedNotDeclared"] == []
    assert out["directAgreesWithApi"] is False


def test_the_quota_lock_is_exclusive_and_released(tmp_path: Path) -> None:
    """One hourly api.data.gov budget is shared with govinfo, so two runs must not overlap.

    On 2026-09-05 a census was launched into a running sample and both drew on
    that budget for nine minutes, because neither tool looked for the other.
    """
    lock = tmp_path / "api-quota.lock"
    with api_quota_lock(lock, holder="first", purpose="sample", wait=False):
        assert lock.exists()
        assert "holder=first" in lock.read_text()
        with pytest.raises(SystemExit):
            with api_quota_lock(lock, holder="second", purpose="census", wait=False):
                pass
    assert not lock.exists()

    # A run that takes no metered path takes no lock: passing None is the way
    # the unmetered-only mode says "this costs nothing from the shared budget".
    with api_quota_lock(None, holder="direct-only", purpose="unmetered", wait=False):
        assert not lock.exists()
