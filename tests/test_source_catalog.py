from __future__ import annotations

from pathlib import Path

import pytest

from docspec.adapters.source_catalog import LocalFileContentFetcher, LocalJsonlSourceCatalog
from docspec.domain.content import CandidateFile, SourceItem, SourceItemState
from docspec.errors import IntegrityError, LimitExceededError


def _items() -> list[SourceItem]:
    return [
        SourceItem("a", "v1", (CandidateFile("main", "a.txt", "text/plain", expected_size=1),)),
        SourceItem("b", "v2", (), SourceItemState.DELETED),
    ]


def test_source_catalog_round_trips_complete_stable_distribution(tmp_path: Path) -> None:
    catalogs = LocalJsonlSourceCatalog(tmp_path / "catalogs")
    reference = catalogs.write(
        _items(),
        partitions=("000", "001"),
        coverage={"selected": 2, "complete": True},
    )

    summary = catalogs.describe(reference)
    assert summary.item_count == 2
    assert summary.state_counts == {"active": 1, "deleted": 1, "excluded": 0}
    assert summary.partitions == ("000", "001")
    assert [item.item_id for item in catalogs.stream(reference)] == ["a", "b"]
    opened = catalogs.open(reference)
    assert opened.summary == summary
    assert [item.item_id for item in opened.items] == ["a", "b"]

    distribution = (catalogs.root / reference.locator).parent
    assert {path.name for path in distribution.iterdir()} == {"catalog.json", "items.jsonl"}
    (distribution / "extra.json").write_text("{}")
    with pytest.raises(IntegrityError, match="missing, extra, or symlinked"):
        catalogs.verify(reference)


def test_source_catalog_rejects_unstable_or_ambiguous_input(tmp_path: Path) -> None:
    catalogs = LocalJsonlSourceCatalog(tmp_path / "catalogs")
    with pytest.raises(IntegrityError, match="strictly ordered"):
        catalogs.write(list(reversed(_items())))
    with pytest.raises(IntegrityError, match="more than one current record"):
        catalogs.write(
            [
                SourceItem("a", "v1", (CandidateFile("main", "a.txt", "text/plain"),)),
                SourceItem("a", "v2", (CandidateFile("main", "a.txt", "text/plain"),)),
            ]
        )


def test_source_catalog_change_set_pins_its_verified_base(tmp_path: Path) -> None:
    catalogs = LocalJsonlSourceCatalog(tmp_path / "catalogs")
    base = catalogs.write(_items())
    changed_item = SourceItem(
        "a",
        "v2",
        (CandidateFile("main", "a-v2.txt", "text/plain", expected_size=2),),
    )
    change_set = catalogs.write(
        (changed_item,),
        kind="change-set",
        base_catalog=base,
        coverage={"changed": 1, "complete": True},
    )

    summary = catalogs.verify(change_set)
    assert summary.kind == "change-set"
    assert summary.base_catalog == base
    assert summary.item_count == 1
    assert tuple(catalogs.stream(change_set)) == (changed_item,)

    with pytest.raises(ValueError, match="identify its base"):
        catalogs.write((changed_item,), kind="change-set")


def test_local_file_fetcher_is_contained_streamed_and_receipted(tmp_path: Path) -> None:
    source_root = tmp_path / "source"
    source_root.mkdir()
    (source_root / "document.txt").write_bytes(b"exact bytes")
    fetcher = LocalFileContentFetcher(source_root, chunk_size=3)
    candidate = CandidateFile(
        "main",
        "document.txt",
        "text/plain",
        expected_size=11,
        transport_version="fixture-v1",
    )

    result = fetcher.fetch(candidate, max_bytes=20, task_id="task-1", attempt_id="attempt-1")
    assert b"".join(result.chunks) == b"exact bytes"
    assert result.metadata.downloader_id == fetcher.downloader_id
    assert result.metadata.downloader_configuration_digest == fetcher.configuration_digest
    assert result.metadata.transport_version == "fixture-v1"
    assert result.metadata.task_id == "task-1"
    assert result.metadata.attempt_id == "attempt-1"

    with pytest.raises(LimitExceededError):
        fetcher.fetch(candidate, max_bytes=5, task_id="task-2", attempt_id="attempt-2")

    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    (source_root / "link.txt").symlink_to(outside)
    linked = CandidateFile("linked", "link.txt", "text/plain")
    with pytest.raises(IntegrityError):
        fetcher.fetch(linked, max_bytes=20, task_id="task-3", attempt_id="attempt-3")
