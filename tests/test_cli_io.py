"""Input limits apply to bytes read, including files that grow after stat."""

from __future__ import annotations

from io import BytesIO
from pathlib import Path

import pytest

from docspec.cli_io import CliError, SourceCatalogCliError, read_bytes


@pytest.mark.parametrize("error_type", [CliError, SourceCatalogCliError])
def test_a_growing_input_is_bounded_and_keeps_the_command_error_type(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, error_type: type[Exception]
) -> None:
    path = tmp_path / "request.json"
    path.write_bytes(b"{}")
    requested_sizes: list[int] = []

    class GrowingFile(BytesIO):
        def read(self, size: int = -1) -> bytes:
            requested_sizes.append(size)
            return super().read(size)

    original_open = Path.open

    def open_after_growth(current: Path, *args, **kwargs):
        if current == path:
            return GrowingFile(b"x" * 100)
        return original_open(current, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_after_growth)
    with pytest.raises(error_type, match="request exceeds the 4-byte limit"):
        read_bytes(path, label="request", max_bytes=4, error_type=error_type)
    assert requested_sizes == [5]
