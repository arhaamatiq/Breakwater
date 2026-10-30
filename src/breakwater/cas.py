"""Content-addressed bundle store: a thin alias over storage's blob API.

Bundles go wherever artifacts go (BW_ARTIFACTS: a local directory, or an S3
bucket shared by workers on several machines). Reads re-verify the sha256,
so a corrupted or truncated blob is an error, never a silently different
verifier input."""
import os

from . import storage

BW_HOME = os.path.expanduser(os.environ.get("BW_HOME", "~/.breakwater"))


def put(data: bytes) -> str:
    return storage.put_blob(data)


def get(digest: str) -> bytes:
    return storage.get_blob(digest)


def exists(digest: str) -> bool:
    return storage.blob_exists(digest)
