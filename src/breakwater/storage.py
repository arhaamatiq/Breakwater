"""Artifact storage with two backends, selected by BW_ARTIFACTS:

  file://<dir>   default file://~/.breakwater/artifacts: a local directory, no services
  s3://<bucket>  any S3 API (MinIO in docker-compose) via the BW_S3_* endpoint/credentials

Two APIs over either backend:
  put_bytes(key, data) -> uri, get_bytes(key), get_uri(uri)
      keyed artifacts (per-attempt logs and outputs); the uri is self-describing,
      so get_uri works no matter which backend is configured when it is read.
  put_blob(data) -> "sha256:<hex>", get_blob(digest), blob_exists(digest)
      content-addressed: identical bytes are stored once, and every read is
      re-hashed, so a corrupted or tampered blob raises instead of being trusted.

File writes are atomic (temp file in the same directory + fsync + os.replace):
a crashed writer leaves the old object or none, never a torn one. Concurrent
writers of the same blob race harmlessly, since they write identical bytes.

Sync on purpose (boto3 is sync); async callers use asyncio.to_thread.
"""
import contextlib
import hashlib
import os
import re
import tempfile
import threading
from pathlib import Path
from urllib.parse import unquote, urlparse

from .config import CFG


class BlobIntegrityError(Exception):
    """Stored bytes do not hash to the digest they are stored under."""


def parse_location(url: str) -> tuple[str, str]:
    """BW_ARTIFACTS value -> ("file", absolute directory) or ("s3", bucket).
    A bare path (no scheme) is a file location."""
    if "://" not in url:
        return "file", os.path.abspath(os.path.expanduser(url))
    scheme, rest = url.split("://", 1)
    if scheme == "file":
        if not rest:
            raise ValueError("file:// location needs a directory")
        return "file", os.path.abspath(os.path.expanduser(rest))
    if scheme == "s3":
        bucket, _, prefix = rest.partition("/")
        if not bucket or prefix.strip("/"):
            raise ValueError(f"expected s3://<bucket> (no key prefix), got {url!r}")
        return "s3", bucket
    raise ValueError(f"unsupported artifact store {url!r}: use file://<dir> or s3://<bucket>")


# ---------------------------------------------------------------- backends

class FileStore:
    def __init__(self, root: str | os.PathLike):
        self.root = Path(root)

    def describe(self) -> str:
        return f"file://{self.root}"

    def ensure(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)

    def path(self, key: str) -> Path:
        parts = key.split("/")
        if any(p in ("", ".", "..") for p in parts):  # also rejects absolute keys
            raise ValueError(f"invalid artifact key {key!r}")
        return self.root.joinpath(*parts)

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        path = self.path(key)
        atomic_write(path, data)
        return path.as_uri()

    def get(self, key: str) -> bytes:
        return self.path(key).read_bytes()

    def exists(self, key: str) -> bool:
        return self.path(key).is_file()


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            os.fchmod(f.fileno(), 0o644)  # mkstemp's 0600 would hide artifacts from other readers
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(tmp)
        raise


_s3_client = None
_s3_lock = threading.Lock()  # boto3's default session is not thread-safe to build clients from


def s3_client():
    global _s3_client
    with _s3_lock:
        if _s3_client is None:
            import boto3  # lazy: the file backend never pays for (or needs) boto3
            from botocore.client import Config as BotoConfig
            _s3_client = boto3.client(
                "s3",
                endpoint_url=CFG.s3_endpoint,
                aws_access_key_id=CFG.s3_key,
                aws_secret_access_key=CFG.s3_secret,
                config=BotoConfig(signature_version="s3v4", retries={"max_attempts": 3}),
                region_name="us-east-1",
            )
        return _s3_client


class S3Store:
    def __init__(self, bucket: str):
        self.bucket = bucket

    def describe(self) -> str:
        return f"s3://{self.bucket} at {CFG.s3_endpoint}"

    def ensure(self) -> None:
        from botocore.exceptions import ClientError
        c = s3_client()
        try:
            c.head_bucket(Bucket=self.bucket)
        except ClientError:
            c.create_bucket(Bucket=self.bucket)

    def put(self, key: str, data: bytes, content_type: str | None = None) -> str:
        s3_client().put_object(Bucket=self.bucket, Key=key, Body=data,
                               ContentType=content_type or "application/octet-stream")
        return f"s3://{self.bucket}/{key}"

    def get(self, key: str) -> bytes:
        return s3_client().get_object(Bucket=self.bucket, Key=key)["Body"].read()

    def exists(self, key: str) -> bool:
        from botocore.exceptions import ClientError
        try:
            s3_client().head_object(Bucket=self.bucket, Key=key)
            return True
        except ClientError as e:
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey", "NotFound"):
                return False
            raise


def open_store(url: str) -> FileStore | S3Store:
    kind, loc = parse_location(url)
    return FileStore(loc) if kind == "file" else S3Store(loc)


_store = None


def store() -> FileStore | S3Store:
    """The configured backend (BW_ARTIFACTS), built on first use."""
    global _store
    if _store is None:
        _store = open_store(CFG.artifacts)
    return _store


def configure(url: str | None = None) -> FileStore | S3Store:
    """Point this process at another store (tests, tools); None re-reads BW_ARTIFACTS."""
    global _store
    _store = open_store(url or CFG.artifacts)
    return _store


def ensure_store() -> str:
    """Create the directory or bucket. Only an s3:// store ever touches S3."""
    s = store()
    s.ensure()
    return s.describe()


def ensure_bucket() -> None:
    """Back-compat alias for the S3-era setup step."""
    ensure_store()


# ---------------------------------------------------------------- keyed artifacts

def put_bytes(key: str, data: bytes, content_type: str = "application/octet-stream") -> str:
    return store().put(key, data, content_type)


def get_bytes(key: str) -> bytes:
    return store().get(key)


def get_uri(uri: str) -> bytes:
    """Read back a uri returned by put_bytes, from whichever backend wrote it."""
    u = urlparse(uri)
    if u.scheme == "file":
        return Path(unquote(u.path)).read_bytes()
    if u.scheme == "s3":
        return S3Store(u.netloc).get(u.path.lstrip("/"))
    raise ValueError(f"not an artifact uri: {uri!r}")


# ---------------------------------------------------------------- content-addressed blobs

_DIGEST = re.compile(r"sha256:([0-9a-f]{64})")


def _blob_key(digest: str) -> tuple[str, str]:
    m = _DIGEST.fullmatch(digest.strip().lower()) if isinstance(digest, str) else None
    if not m:
        raise ValueError(f"expected 'sha256:<64 hex>', got {digest!r}")
    hexd = m.group(1)
    return f"blobs/sha256/{hexd[:2]}/{hexd}", hexd


def put_blob(data: bytes) -> str:
    """Store bytes under their sha256 (no-op if already present); returns 'sha256:<hex>'."""
    hexd = hashlib.sha256(data).hexdigest()
    key, _ = _blob_key(f"sha256:{hexd}")
    s = store()
    if not s.exists(key):
        s.put(key, data)
    return f"sha256:{hexd}"


def get_blob(digest: str) -> bytes:
    """Fetch a blob and verify it still hashes to its digest (raises BlobIntegrityError)."""
    key, hexd = _blob_key(digest)
    data = store().get(key)
    actual = hashlib.sha256(data).hexdigest()
    if actual != hexd:
        raise BlobIntegrityError(f"blob {digest} is corrupt: content hashes to sha256:{actual}")
    return data


def blob_exists(digest: str) -> bool:
    key, _ = _blob_key(digest)
    return store().exists(key)
