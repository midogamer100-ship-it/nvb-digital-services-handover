"""Private object storage for generated PDFs.

Durable output never lives on container-local disk. The bucket must be private (no public
policy); downloads are always streamed through the authorised API.
"""

from __future__ import annotations

import hashlib
import threading
from typing import Protocol

from nvb_statements.config import Settings, read_secret


class StorageError(Exception):
    pass


class ObjectStore(Protocol):
    def put(self, key: str, data: bytes, sha256: str) -> None: ...
    def get(self, key: str) -> bytes: ...
    def exists(self, key: str) -> bool: ...
    def list(self, prefix: str) -> list[str]: ...
    def delete(self, key: str) -> None: ...
    def ping(self) -> bool: ...


def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


class InMemoryStore:
    """Test/local double with the same semantics (overwrite on same key)."""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.puts = 0
        self._lock = threading.Lock()
        self.fail_next_put = False

    def put(self, key: str, data: bytes, sha256: str) -> None:
        if sha256_hex(data) != sha256:
            raise StorageError("checksum mismatch before upload")
        with self._lock:
            if self.fail_next_put:
                self.fail_next_put = False
                raise StorageError("simulated storage outage")
            self.objects[key] = data
            self.puts += 1

    def get(self, key: str) -> bytes:
        try:
            return self.objects[key]
        except KeyError:
            raise StorageError("object not found") from None

    def exists(self, key: str) -> bool:
        return key in self.objects

    def list(self, prefix: str) -> list[str]:
        return sorted(k for k in self.objects if k.startswith(prefix))

    def delete(self, key: str) -> None:
        self.objects.pop(key, None)

    def ping(self) -> bool:
        return True


class S3Store:
    def __init__(self, settings: Settings) -> None:
        import boto3
        from botocore.config import Config

        self.bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            region_name=settings.s3_region,
            aws_access_key_id=read_secret(settings.s3_access_key_file),
            aws_secret_access_key=read_secret(settings.s3_secret_key_file),
            verify=settings.s3_ca_file or True,  # TLS verification is never disabled
            config=Config(retries={"max_attempts": 3, "mode": "standard"}, connect_timeout=5, read_timeout=30,
                          s3={"addressing_style": "path"}),
        )

    def put(self, key: str, data: bytes, sha256: str) -> None:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            self._client.put_object(Bucket=self.bucket, Key=key, Body=data, ContentType="application/pdf",
                                    Metadata={"sha256": sha256})
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(type(exc).__name__) from None

    def get(self, key: str) -> bytes:
        from botocore.exceptions import BotoCoreError, ClientError

        try:
            return self._client.get_object(Bucket=self.bucket, Key=key)["Body"].read()
        except (BotoCoreError, ClientError) as exc:
            raise StorageError(type(exc).__name__) from None

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError

        try:
            self._client.head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError:
            return False

    def list(self, prefix: str) -> list[str]:
        keys, token = [], None
        while True:
            kw = {"Bucket": self.bucket, "Prefix": prefix, "MaxKeys": 1000}
            if token:
                kw["ContinuationToken"] = token
            page = self._client.list_objects_v2(**kw)
            keys += [o["Key"] for o in page.get("Contents", [])]
            if not page.get("IsTruncated"):
                return keys
            token = page["NextContinuationToken"]

    def delete(self, key: str) -> None:
        self._client.delete_object(Bucket=self.bucket, Key=key)

    def ping(self) -> bool:
        try:
            self._client.head_bucket(Bucket=self.bucket)
            return True
        except Exception:  # noqa: BLE001 - readiness probe only
            return False


def build_store(settings: Settings) -> ObjectStore:
    if not settings.s3_endpoint_url:
        raise StorageError("STATEMENTS_S3_ENDPOINT_URL is not configured")
    return S3Store(settings)
