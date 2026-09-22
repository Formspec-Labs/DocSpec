"""Processor receipts must reject secret-like content while diagnostics redact it.

``require_secret_free`` raises IntegrityError; common provider credential patterns are covered as defense
in depth. ``redact`` and ``redact_text`` replace matching values and text with REDACTED_SECRET.
"""
import pytest
from docspec.domain.security import REDACTED_SECRET, redact, redact_text, require_secret_free
from docspec.errors import IntegrityError


def test_processor_receipts_reject_secrets_and_diagnostics_redact_them() -> None:
    with pytest.raises(IntegrityError, match="secret-like content"):
        require_secret_free({"message": "Bearer abcdefghijklmnopqrstuvwxyz"}, label="fixture")

    assert redact_text("failure: password=correct-horse-battery-staple") == REDACTED_SECRET
    assert redact(
        {
            "apiKey": "ordinary-looking-value",
            "message": "Bearer abcdefghijklmnopqrstuvwxyz",
            "safe": "retained",
        }
    ) == {
        "apiKey": REDACTED_SECRET,
        "message": REDACTED_SECRET,
        "safe": "retained",
    }


@pytest.mark.parametrize(
    "secret",
    (
        "ghp_abcdefghijklmnopqrstuvwxyz123456",
        "sk" + "_live_" + "abcdefghijklmnopqrstuvwxyz",
        "eyJabcdefghi.abcdefghijklmnop.abcdefghijklmnop",
        "aws_secret_access_key=abcdefghijklmnopqrstuvwxyz1234567890ABCD",
        "-----BEGIN PRIVATE KEY-----",
    ),
)
def test_common_provider_credentials_are_detected_as_defense_in_depth(secret: str) -> None:
    with pytest.raises(IntegrityError, match="secret-like content"):
        require_secret_free({"diagnostic": secret}, label="fixture")
