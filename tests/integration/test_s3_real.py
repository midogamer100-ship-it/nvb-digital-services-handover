"""Real S3-compatible object storage: private bucket, idempotent upload, integrity, outages."""

from __future__ import annotations

import os
import socket
import uuid

import httpx
import pytest

from nvb_statements.storage import S3Store, StorageError, sha256_hex


@pytest.fixture
def store(stm_settings):
    return S3Store(stm_settings)


def test_upload_download_list_delete_roundtrip(store):
    rid = str(uuid.uuid4())
    key = f"statements/{rid}/{'a' * 64}.pdf"
    data = b"%PDF-1.4 synthetic " + os.urandom(2048)
    assert store.ping() is True
    assert store.exists(key) is False
    store.put(key, data, sha256_hex(data))
    assert store.exists(key) is True and store.get(key) == data
    assert store.list(f"statements/{rid}/") == [key]
    store.delete(key)
    assert store.exists(key) is False and store.list(f"statements/{rid}/") == []
    with pytest.raises(StorageError):
        store.get(key)


def test_reupload_of_the_same_key_overwrites_one_logical_object(store):
    """Recovery after 'upload succeeded, DB commit did not': the retry writes the SAME key."""
    rid = str(uuid.uuid4())
    key = f"statements/{rid}/{'b' * 64}.pdf"
    first, second = b"%PDF first attempt", b"%PDF second attempt (same dataset)"
    store.put(key, first, sha256_hex(first))
    store.put(key, second, sha256_hex(second))
    assert store.list(f"statements/{rid}/") == [key] and store.get(key) == second
    store.delete(key)


def test_bucket_is_private_anonymous_requests_are_denied(store, stm_settings):
    rid = str(uuid.uuid4())
    key = f"statements/{rid}/{'c' * 64}.pdf"
    data = b"%PDF private"
    store.put(key, data, sha256_hex(data))
    base = f"{stm_settings.s3_endpoint_url}/{stm_settings.s3_bucket}"
    verify = stm_settings.s3_ca_file or True
    try:
        anon_get = httpx.get(f"{base}/{key}", verify=verify, timeout=10)
        anon_list = httpx.get(f"{base}?list-type=2", verify=verify, timeout=10)
        anon_put = httpx.put(f"{base}/statements/{rid}/evil.pdf", content=b"x", verify=verify, timeout=10)
        assert anon_get.status_code in (401, 403) and data not in anon_get.content
        assert anon_list.status_code in (401, 403)
        assert anon_put.status_code in (401, 403)
        assert store.list(f"statements/{rid}/") == [key]
    finally:
        store.delete(key)


def test_wrong_credentials_are_rejected(stm_settings, tmp_path):
    bad = tmp_path / "secret"
    bad.write_text("not-the-secret-key-not-the-secret-key", encoding="utf-8")
    wrong = S3Store(stm_settings.model_copy(update={"s3_secret_key_file": str(bad)}))
    with pytest.raises(StorageError):
        wrong.put("statements/x/y.pdf", b"x", sha256_hex(b"x"))
    assert wrong.ping() is False


def test_storage_outage_raises_storage_error(stm_settings):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    down = S3Store(stm_settings.model_copy(update={"s3_endpoint_url": f"http://127.0.0.1:{port}"}))
    with pytest.raises(StorageError):
        down.put("statements/x/y.pdf", b"x", sha256_hex(b"x"))
    with pytest.raises(StorageError):
        down.get("statements/x/y.pdf")
    assert down.ping() is False
