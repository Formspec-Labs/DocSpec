from __future__ import annotations

import hashlib
import sys
from types import SimpleNamespace
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO

import pytest

from docspec.adapters.s3_blob import S3BlobStoreConfig, S3BlobStoreError, S3ContentAddressedBlobStore
from docspec.adapters.storage import LocalContentAddressedBlobStore
from docspec.domain.identity import sha256_digest
from docspec.domain.references import BlobRef
from docspec.errors import IntegrityError, LimitExceededError


class _S3Error(Exception):
    def __init__(self, code: str, status: int) -> None:
        super().__init__(f"provider error {code}")
        self.response = {
            "Error": {"Code": code, "Message": "provider-only detail"},
            "ResponseMetadata": {"HTTPStatusCode": status},
        }


class _StreamingBody:
    def __init__(self, payload: bytes) -> None:
        self._payload = payload
        self._position = 0
        self.closed = False
        self.read_sizes: list[int | None] = []

    def read(self, amount: int | None = None) -> bytes:
        self.read_sizes.append(amount)
        if self.closed:
            raise RuntimeError("body is closed")
        if amount is None:
            end = len(self._payload)
        else:
            end = min(self._position + amount, len(self._payload))
        result = self._payload[self._position : end]
        self._position = end
        return result

    def close(self) -> None:
        self.closed = True


@dataclass
class _StoredObject:
    payload: bytes
    content_type: str
    metadata: dict[str, str]


class _FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], _StoredObject] = {}
        self.put_requests: list[dict[str, Any]] = []
        self.get_requests: list[dict[str, Any]] = []
        self.bodies: list[_StreamingBody] = []
        self.race_on_next_put = False
        self.head_error: Exception | None = None

    def head_object(self, *, Bucket: str, Key: str) -> dict[str, Any]:  # noqa: N803 - boto3 API shape
        if self.head_error is not None:
            raise self.head_error
        try:
            stored = self.objects[(Bucket, Key)]
        except KeyError as error:
            raise _S3Error("NoSuchKey", 404) from error
        return {
            "ContentLength": len(stored.payload),
            "ContentType": stored.content_type,
            "Metadata": dict(stored.metadata),
            "ETag": '"opaque-provider-etag"',
        }

    def put_object(
        self,
        *,
        Bucket: str,  # noqa: N803 - boto3 API shape
        Key: str,  # noqa: N803 - boto3 API shape
        Body: BinaryIO,  # noqa: N803 - boto3 API shape
        ContentLength: int,  # noqa: N803 - boto3 API shape
        ContentType: str,  # noqa: N803 - boto3 API shape
        Metadata: dict[str, str],  # noqa: N803 - boto3 API shape
        IfNoneMatch: str,  # noqa: N803 - boto3 API shape
    ) -> dict[str, Any]:
        payload = Body.read()
        request = {
            "Bucket": Bucket,
            "Key": Key,
            "ContentLength": ContentLength,
            "ContentType": ContentType,
            "Metadata": dict(Metadata),
            "IfNoneMatch": IfNoneMatch,
        }
        self.put_requests.append(request)
        assert len(payload) == ContentLength
        if self.race_on_next_put:
            self.race_on_next_put = False
            self.objects[(Bucket, Key)] = _StoredObject(payload, ContentType, dict(Metadata))
            raise _S3Error("PreconditionFailed", 412)
        if IfNoneMatch == "*" and (Bucket, Key) in self.objects:
            raise _S3Error("PreconditionFailed", 412)
        self.objects[(Bucket, Key)] = _StoredObject(payload, ContentType, dict(Metadata))
        return {"ETag": '"not-an-integrity-digest"'}

    def get_object(self, **request: Any) -> dict[str, Any]:
        self.get_requests.append(dict(request))
        bucket = request["Bucket"]
        key = request["Key"]
        try:
            stored = self.objects[(bucket, key)]
        except KeyError as error:
            raise _S3Error("NoSuchKey", 404) from error
        payload = stored.payload
        response: dict[str, Any] = {
            "ContentType": stored.content_type,
            "Metadata": dict(stored.metadata),
        }
        range_header = request.get("Range")
        if range_header is not None:
            interval = range_header.removeprefix("bytes=")
            first, last = (int(value) for value in interval.split("-", maxsplit=1))
            payload = payload[first : last + 1]
            response["ContentRange"] = f"bytes {first}-{last}/{len(stored.payload)}"
        body = _StreamingBody(payload)
        self.bodies.append(body)
        response["ContentLength"] = len(payload)
        response["Body"] = body
        return response


def _s3_store(
    tmp_path: Path,
    *,
    prefix: str = "",
    max_blob_bytes: int = 32,
) -> tuple[S3ContentAddressedBlobStore, _FakeS3Client]:
    client = _FakeS3Client()
    store = S3ContentAddressedBlobStore(
        client,
        bucket="documents",
        prefix=prefix,
        config=S3BlobStoreConfig(
            max_blob_bytes=max_blob_bytes,
            transfer_chunk_bytes=3,
            staging_directory=tmp_path / "staging",
        ),
    )
    return store, client


@pytest.mark.parametrize("profile", ["local", "amazon-s3", "r2-s3-compatible"])
def test_blob_store_behavioral_contract(profile: str, tmp_path: Path) -> None:
    if profile == "local":
        store = LocalContentAddressedBlobStore(tmp_path / "local", max_blob_bytes=32)
        client = None
    else:
        store, client = _s3_store(tmp_path, prefix="tenant-a" if profile.startswith("r2") else "")

    payload = b"exact bytes"
    digest = sha256_digest(payload)
    first = store.put_if_absent(
        [b"exact ", b"bytes"],
        media_type="text/plain",
        expected_digest=digest,
        expected_size=len(payload),
    )
    second = store.put_if_absent([payload], media_type="text/plain")

    assert first == second
    assert first.locator == f"objects/sha256/{digest[7:9]}/{digest[7:]}"
    assert store.stat(first) == first
    assert b"".join(store.read(first, chunk_size=3, max_bytes=len(payload))) == payload
    assert store.read_range(first, start=6, end=11) == b"bytes"
    store.verify(first)
    materialized = store.materialize(first, tmp_path / profile / "work", "nested/file.txt")
    assert materialized.read_bytes() == payload

    if client is not None:
        assert len(client.put_requests) == 1
        assert client.put_requests[0]["IfNoneMatch"] == "*"
        expected_key = first.locator if profile == "amazon-s3" else f"tenant-a/{first.locator}"
        assert client.put_requests[0]["Key"] == expected_key
        assert all(body.closed for body in client.bodies)


def test_put_is_bounded_and_checks_declared_identity_before_upload(tmp_path: Path) -> None:
    store, client = _s3_store(tmp_path, max_blob_bytes=8)

    with pytest.raises(LimitExceededError, match="8-byte write limit"):
        store.put_if_absent([b"1234", b"56789"], media_type="application/octet-stream")
    with pytest.raises(IntegrityError, match="expected digest"):
        store.put_if_absent(
            [b"safe"],
            media_type="text/plain",
            expected_digest=f"sha256:{'0' * 64}",
        )
    with pytest.raises(IntegrityError, match="expected size"):
        store.put_if_absent([b"safe"], media_type="text/plain", expected_size=5)

    assert client.objects == {}
    assert client.put_requests == []
    assert list((tmp_path / "staging").iterdir()) == []


def test_conditional_create_accepts_only_an_identical_concurrent_object(tmp_path: Path) -> None:
    store, client = _s3_store(tmp_path)
    client.race_on_next_put = True

    reference = store.put_if_absent([b"concurrent"], media_type="text/plain")

    assert b"".join(store.read(reference)) == b"concurrent"
    assert len(client.put_requests) == 1

    stored = client.objects[("documents", reference.locator)]
    stored.payload = b"Concurrent"
    with pytest.raises(IntegrityError, match="bytes differ"):
        store.put_if_absent([b"concurrent"], media_type="text/plain")


def test_stat_and_verify_reject_tampered_metadata_and_bytes(tmp_path: Path) -> None:
    store, client = _s3_store(tmp_path)
    reference = store.put_if_absent([b"trusted"], media_type="text/plain")
    stored = client.objects[("documents", reference.locator)]

    stored.metadata["docspec-sha256"] = f"sha256:{'f' * 64}"
    with pytest.raises(IntegrityError, match="digest metadata"):
        store.stat(reference)

    stored.metadata["docspec-sha256"] = reference.digest
    stored.payload = b"Trusted"
    with pytest.raises(IntegrityError, match="bytes differ"):
        store.verify(reference)


def test_reads_are_bounded_streaming_and_ranges_are_half_open(tmp_path: Path) -> None:
    store, client = _s3_store(tmp_path)
    reference = store.put_if_absent([b"abcdefghij"], media_type="application/octet-stream")

    with pytest.raises(LimitExceededError, match="9-byte read limit"):
        list(store.read(reference, max_bytes=9))
    assert store.read_range(reference, start=2, end=7) == b"cdefg"
    assert client.get_requests[-1]["Range"] == "bytes=2-6"
    assert client.bodies[-1].read_sizes == [3, 2, 1]
    assert store.read_range(reference, start=4, end=4) == b""
    with pytest.raises(ValueError, match="half-open interval"):
        store.read_range(reference, start=-1, end=2)
    with pytest.raises(ValueError, match="half-open interval"):
        store.read_range(reference, start=0, end=11)


def test_materialization_is_contained_and_never_replaces_files(tmp_path: Path) -> None:
    store, _ = _s3_store(tmp_path)
    reference = store.put_if_absent([b"safe"], media_type="text/plain")
    root = tmp_path / "work"

    destination = store.materialize(reference, root, "nested/file.txt")
    assert destination.read_bytes() == b"safe"
    with pytest.raises(IntegrityError, match="refusing to replace"):
        store.materialize(reference, root, "nested/file.txt")
    with pytest.raises(ValueError, match="contained relative path"):
        store.materialize(reference, root, "../outside.txt")

    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "linked").symlink_to(outside, target_is_directory=True)
    with pytest.raises(IntegrityError, match="symlink"):
        store.materialize(reference, root, "linked/file.txt")


def test_provider_failures_are_normalized_at_the_adapter_boundary(tmp_path: Path) -> None:
    store, client = _s3_store(tmp_path)
    reference = BlobRef(
        locator=f"objects/sha256/aa/{'a' * 64}",
        digest=f"sha256:{'a' * 64}",
        byte_size=1,
        media_type="application/octet-stream",
    )
    client.head_error = _S3Error("AccessDenied", 403)

    with pytest.raises(S3BlobStoreError, match="S3 stat failed") as raised:
        store.stat(reference)

    assert not isinstance(raised.value, _S3Error)


def test_configuration_and_reference_validation_fail_closed(tmp_path: Path) -> None:
    client = _FakeS3Client()
    with pytest.raises(ValueError, match="forward slashes"):
        S3ContentAddressedBlobStore(client, bucket="documents", prefix=r"bad\prefix")
    with pytest.raises(ValueError, match="contained relative path"):
        S3ContentAddressedBlobStore(client, bucket="documents", prefix="../bad")
    with pytest.raises(ValueError, match="positive integer"):
        S3BlobStoreConfig(max_blob_bytes=0)
    with pytest.raises(ValueError, match="5 GiB"):
        S3BlobStoreConfig(max_blob_bytes=5 * 1024**3 + 1)

    store, _ = _s3_store(tmp_path)
    digest = hashlib.sha256(b"x").hexdigest()
    wrong_locator = BlobRef(
        locator=f"objects/sha256/00/{digest}",
        digest=f"sha256:{digest}",
        byte_size=1,
        media_type="text/plain",
    )
    with pytest.raises(IntegrityError, match="locator"):
        store.stat(wrong_locator)


def test_boto3_factory_is_lazy_and_uses_the_same_injected_client_boundary(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _FakeS3Client()
    calls: list[tuple[str, object]] = []

    class _Session:
        def __init__(self, *, profile_name: str | None = None) -> None:
            calls.append(("profile", profile_name))

        def client(self, service: str, **options: Any) -> _FakeS3Client:
            calls.append((service, options))
            return client

    monkeypatch.setitem(sys.modules, "boto3", SimpleNamespace(Session=_Session))
    store = S3ContentAddressedBlobStore.from_boto3(
        bucket="documents",
        prefix="r2",
        endpoint_url="https://account.r2.example",
        region_name="auto",
        profile_name="docspec",
        client_options={"verify": True},
        config=S3BlobStoreConfig(staging_directory=tmp_path / "staging"),
    )

    reference = store.put_if_absent([b"factory"], media_type="text/plain")
    assert b"".join(store.read(reference)) == b"factory"
    assert calls == [
        ("profile", "docspec"),
        (
            "s3",
            {
                "verify": True,
                "endpoint_url": "https://account.r2.example",
                "region_name": "auto",
            },
        ),
    ]
