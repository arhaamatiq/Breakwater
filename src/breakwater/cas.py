"""Content-addressed blob store for bundles (local filesystem).

Blobs live at $BW_HOME/blobs/sha256/<2>/<hex>; writes are atomic (temp file +
os.replace) and reads re-verify the hash, so a corrupted or truncated blob is
an error, never a silently different verifier input."""
import hashlib
import os
import tempfile

BW_HOME = os.path.expanduser(os.environ.get("BW_HOME", "~/.breakwater"))


def _path(digest: str) -> str:
    algo, _, hexd = digest.partition(":")
    if algo != "sha256" or len(hexd) != 64:
        raise ValueError(f"bad digest {digest!r}")
    return os.path.join(BW_HOME, "blobs", "sha256", hexd[:2], hexd)


def put(data: bytes) -> str:
    digest = "sha256:" + hashlib.sha256(data).hexdigest()
    p = _path(digest)
    if not os.path.exists(p):
        os.makedirs(os.path.dirname(p), exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(p))
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, p)
    return digest


def get(digest: str) -> bytes:
    with open(_path(digest), "rb") as f:
        data = f.read()
    if "sha256:" + hashlib.sha256(data).hexdigest() != digest:
        raise IOError(f"blob {digest} failed hash verification")
    return data


def exists(digest: str) -> bool:
    return os.path.exists(_path(digest))
