"""Read S3 error metadata without importing a provider SDK.

Callers decide whether the code or status means missing, changed, or retryable
for their operation; only the provider response parsing is shared.
"""

from collections.abc import Mapping


def provider_error_identity(error: Exception) -> tuple[str | None, int | None]:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None, None
    details = response.get("Error")
    metadata = response.get("ResponseMetadata")
    code = details.get("Code") if isinstance(details, Mapping) else None
    status = metadata.get("HTTPStatusCode") if isinstance(metadata, Mapping) else None
    return str(code) if code is not None else None, status if isinstance(status, int) else None
