"""Artifact storage, file backend: no services needed. (The s3:// backend is the
same boto3 calls as before; it is exercised only against a live MinIO.)"""
import hashlib
import os

import pytest

from breakwater import storage


@pytest.fixture
def store(tmp_path):
    s = storage.configure(f"file://{tmp_path / 'artifacts'}")
    yield s
    storage.configure()  # back to BW_ARTIFACTS for anything that runs after


def test_put_get_bytes_roundtrip_and_uri(store, tmp_path):
    uri = storage.put_bytes("exp-1/trial_a/attempt-1/verify.log", b"ok\n")
    path = tmp_path / "artifacts" / "exp-1" / "trial_a" / "attempt-1" / "verify.log"
    assert uri == path.as_uri() and uri.startswith("file:///")
    assert path.read_bytes() == b"ok\n"
    assert storage.get_bytes("exp-1/trial_a/attempt-1/verify.log") == b"ok\n"
    assert storage.get_uri(uri) == b"ok\n"


def test_get_uri_decodes_escaped_paths(store):
    uri = storage.put_bytes("exp with space/50%/out.json", b"{}")
    assert "%20" in uri and "%25" in uri
    assert storage.get_uri(uri) == b"{}"


@pytest.mark.parametrize("key", ["../escape", "a/../../b", "/abs/path", "a//b", "a/./b", ""])
def test_keys_cannot_escape_the_store(store, key):
    with pytest.raises(ValueError):
        storage.put_bytes(key, b"x")


def test_put_blob_is_content_addressed_and_idempotent(store, tmp_path):
    data = b"verifier output\n" * 100
    hexd = hashlib.sha256(data).hexdigest()
    digest = storage.put_blob(data)
    assert digest == f"sha256:{hexd}"
    path = tmp_path / "artifacts" / "blobs" / "sha256" / hexd[:2] / hexd
    assert path.read_bytes() == data
    before = path.stat()
    assert storage.put_blob(data) == digest                     # skip if present: not rewritten
    after = path.stat()
    assert (after.st_ino, after.st_mtime_ns) == (before.st_ino, before.st_mtime_ns)
    assert storage.blob_exists(digest)
    assert storage.blob_exists(digest.upper().replace("SHA256", "sha256"))  # hex case-insensitive
    assert storage.get_blob(digest) == data
    assert storage.put_blob(b"") == "sha256:" + hashlib.sha256(b"").hexdigest()


def test_get_blob_verifies_hash_on_read(store, tmp_path):
    digest = storage.put_blob(b"the real bytes")
    hexd = digest.split(":")[1]
    (tmp_path / "artifacts" / "blobs" / "sha256" / hexd[:2] / hexd).write_bytes(b"tampered")
    with pytest.raises(storage.BlobIntegrityError):
        storage.get_blob(digest)


def test_missing_blob(store):
    missing = "sha256:" + "0" * 64
    assert not storage.blob_exists(missing)
    with pytest.raises(FileNotFoundError):
        storage.get_blob(missing)


@pytest.mark.parametrize("digest", ["sha256:abc", "md5:" + "0" * 64, "0" * 64,
                                    "sha256:" + "g" * 64, "sha256:../../" + "0" * 58, None])
def test_malformed_digests_are_rejected(store, digest):
    with pytest.raises(ValueError):
        storage.get_blob(digest)
    with pytest.raises(ValueError):
        storage.blob_exists(digest)


def test_writes_are_atomic(store, tmp_path, monkeypatch):
    """A writer that dies before the rename leaves the old object intact and no
    temp files behind; readers never observe a partial write."""
    storage.put_bytes("k/obj", b"version 1")
    target = tmp_path / "artifacts" / "k" / "obj"

    def crash(src, dst):
        assert open(src, "rb").read() == b"version 2, larger"   # fully written, fsynced temp
        raise OSError("simulated crash before rename")
    monkeypatch.setattr(os, "replace", crash)
    with pytest.raises(OSError, match="simulated crash"):
        storage.put_bytes("k/obj", b"version 2, larger")
    monkeypatch.undo()

    assert target.read_bytes() == b"version 1"
    assert sorted(p.name for p in target.parent.iterdir()) == ["obj"]
    storage.put_bytes("k/obj", b"version 2, larger")
    assert target.read_bytes() == b"version 2, larger"
    assert oct(target.stat().st_mode & 0o777) == oct(0o644)


def test_parse_location():
    home = os.path.expanduser("~")
    assert storage.parse_location("file://~/.breakwater/artifacts") == \
        ("file", os.path.join(home, ".breakwater", "artifacts"))
    assert storage.parse_location("file:///var/bw") == ("file", "/var/bw")
    assert storage.parse_location("/var/bw") == ("file", "/var/bw")
    assert storage.parse_location("s3://artifacts") == ("s3", "artifacts")
    assert storage.parse_location("s3://artifacts/") == ("s3", "artifacts")
    for bad in ("s3://", "s3://bucket/prefix", "gs://bucket", "file://"):
        with pytest.raises(ValueError):
            storage.parse_location(bad)


def test_backend_selection(tmp_path):
    try:
        assert isinstance(storage.configure(f"file://{tmp_path}"), storage.FileStore)
        s3 = storage.configure("s3://my-bucket")
        assert isinstance(s3, storage.S3Store) and s3.bucket == "my-bucket"
    finally:
        storage.configure()


def test_file_backend_never_touches_s3(store, monkeypatch):
    """MinIO is optional: with a file:// store, setup and every artifact call
    must work without building an S3 client."""
    monkeypatch.setattr(storage, "s3_client", lambda: pytest.fail("S3 client built"))
    assert storage.ensure_store().startswith("file://")
    storage.put_blob(b"x")
    storage.put_bytes("a/b", b"y")
