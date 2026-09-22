"""Pin canonical JSON admission: duplicate keys and noncanonical bytes are refused by parse_canonical_json."""
import pytest
from docspec.domain.identity import canonical_json_file_bytes, parse_canonical_json
from docspec.errors import IntegrityError


def test_canonical_json_rejects_duplicate_keys_and_noncanonical_bytes() -> None:
    with pytest.raises(IntegrityError, match="duplicate key"):
        parse_canonical_json(b'{"a":1,"a":2}\n')
    with pytest.raises(IntegrityError, match="not canonical"):
        parse_canonical_json(b'{"b":2, "a":1}\n')
    assert parse_canonical_json(canonical_json_file_bytes({"b": 2, "a": 1})) == {"a": 1, "b": 2}
