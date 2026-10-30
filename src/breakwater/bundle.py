"""Output bundles: exactly the bytes a separate-mode verifier is allowed to see.

In Harbor `separate` mode only the declared artifact paths cross from the
agent's container into the verifier's. A bundle is that crossing, frozen:
every file/dir/symlink under the declared paths, normalized the way Harbor's
own upload path normalizes them (tarfile `data` filter: links that escape the
artifact are dropped, setuid/setgid/sticky and group/other-write bits are
cleared, owner is root). Serialized as a deterministic tar (sorted members,
mtime 0, uid/gid 0), so the sha256 of the tar IS the bundle's identity: two
runs that produce the same bytes produce the same digest, and a verifier
trial keyed by (verifier digest, bundle digest) is the same trial no matter
who asks for it.

Bundles are what a Trust Diff replays: a reference solve, a no-op, a
mutant, or an agent's recorded output all reduce to one of these.
"""
from __future__ import annotations

import fnmatch
import hashlib
import io
import os
import posixpath
import stat
import tarfile
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Entry:
    kind: str            # "f" file | "d" dir | "l" symlink
    mode: int = 0o644
    data: bytes = b""
    target: str = ""     # symlink target


def _data_filter_mode(kind: str, mode: int) -> int:
    """Mode after Python's tarfile `data` filter (what Harbor's copy_via_tar
    applies before upload)."""
    if kind == "d":
        return 0o755     # data filter leaves dir modes to the extractor
    if kind == "l":
        return 0o777
    mode &= ~(stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX | stat.S_IWGRP | stat.S_IWOTH)
    mode |= stat.S_IRUSR | stat.S_IWUSR
    if not mode & stat.S_IXUSR:
        mode &= ~(stat.S_IXGRP | stat.S_IXOTH)
    return mode & 0o7777


def under(path: str, root: str) -> bool:
    root = root.rstrip("/") or "/"
    return root == "/" or path == root or path.startswith(root + "/")


@dataclass
class Bundle:
    entries: dict[str, Entry] = field(default_factory=dict)   # abs path -> Entry
    declared: list[str] = field(default_factory=list)         # declared artifact paths

    # ------------------------------------------------------------ identity
    def to_tar(self) -> bytes:
        buf = io.BytesIO()
        with tarfile.open(fileobj=buf, mode="w", format=tarfile.PAX_FORMAT) as tar:
            for path in sorted(self.entries):
                e = self.entries[path]
                ti = tarfile.TarInfo(name=path.lstrip("/"))
                ti.mtime, ti.uid, ti.gid, ti.uname, ti.gname = 0, 0, 0, "", ""
                ti.mode = e.mode
                if e.kind == "d":
                    ti.type = tarfile.DIRTYPE
                    tar.addfile(ti)
                elif e.kind == "l":
                    ti.type, ti.linkname = tarfile.SYMTYPE, e.target
                    tar.addfile(ti)
                else:
                    ti.size = len(e.data)
                    tar.addfile(ti, io.BytesIO(e.data))
        return buf.getvalue()

    @property
    def digest(self) -> str:
        return "sha256:" + hashlib.sha256(self.to_tar()).hexdigest()

    @classmethod
    def from_tar(cls, raw: bytes, declared: list[str] | None = None) -> "Bundle":
        b = cls(declared=list(declared or []))
        with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
            for m in tar.getmembers():
                p = "/" + m.name.lstrip("/")
                if m.isdir():
                    b.entries[p] = Entry("d", m.mode)
                elif m.issym():
                    b.entries[p] = Entry("l", 0o777, target=m.linkname)
                elif m.isfile():
                    b.entries[p] = Entry("f", m.mode, tar.extractfile(m).read())
        return b

    # ------------------------------------------------------------ views
    def files(self) -> dict[str, bytes]:
        return {p: e.data for p, e in self.entries.items() if e.kind == "f"}

    def present(self) -> list[str]:
        return [d for d in self.declared if any(under(p, d) for p in self.entries)]

    def missing(self) -> list[str]:
        return [d for d in self.declared if not any(under(p, d) for p in self.entries)]

    def subtree(self, root: str) -> dict[str, Entry]:
        return {p: e for p, e in self.entries.items() if under(p, root)}

    def manifest(self) -> dict:
        return {
            "digest": self.digest, "declared": self.declared,
            "missing": self.missing(),
            "files": {p: {"sha256": hashlib.sha256(e.data).hexdigest()[:16],
                          "size": len(e.data), "mode": oct(e.mode)}
                      for p, e in sorted(self.entries.items()) if e.kind == "f"},
        }

    # ------------------------------------------------------------ edits
    def replace(self, *, drop: set[str] = frozenset(), drop_under: list[str] = (),
                set_files: dict[str, bytes] | None = None,
                add: dict[str, Entry] | None = None) -> "Bundle":
        """New bundle with entries removed / files rewritten (mode kept)."""
        out = {p: e for p, e in self.entries.items()
               if p not in drop and not any(under(p, r) for r in drop_under)}
        for p, data in (set_files or {}).items():
            old = out.get(p) or self.entries.get(p)
            out[p] = Entry("f", old.mode if old and old.kind == "f" else 0o644, data)
        out.update(add or {})
        return Bundle(out, list(self.declared))


# ------------------------------------------------------------ collection

def add_archive(bundle: Bundle, raw_tar: bytes, request_path: str,
                exclude: list[str] | None = None) -> int:
    """Merge a `docker get_archive(request_path)` stream into the bundle.

    Docker names members relative to request_path's PARENT, so the absolute
    path is join(dirname(request_path without trailing /), member). Applies
    the data-filter normalization and the artifact's exclude globs. Returns
    the number of entries added."""
    parent = posixpath.dirname(request_path.rstrip("/")) or "/"
    root = request_path.rstrip("/") or "/"
    n = 0
    with tarfile.open(fileobj=io.BytesIO(raw_tar)) as tar:
        members = tar.getmembers()
        by_name = {m.name: m for m in members}
        for m in members:
            p = posixpath.normpath(posixpath.join(parent, m.name))
            rel = posixpath.relpath(p, root) if p != root else "."
            if exclude and rel != "." and any(
                    fnmatch.fnmatch(rel, pat) or fnmatch.fnmatch(posixpath.basename(rel), pat)
                    for pat in exclude):
                continue
            if m.isdir():
                bundle.entries[p] = Entry("d", _data_filter_mode("d", m.mode))
            elif m.issym():
                tgt = m.linkname
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(p), tgt))
                if tgt.startswith("/") or not under(resolved, parent):
                    continue  # data filter refuses links that leave the artifact
                bundle.entries[p] = Entry("l", 0o777, target=tgt)
            elif m.islnk():
                src = by_name.get(m.linkname)
                if src is None or not src.isfile():
                    continue
                bundle.entries[p] = Entry("f", _data_filter_mode("f", src.mode),
                                          tar.extractfile(src).read())
            elif m.isfile():
                bundle.entries[p] = Entry("f", _data_filter_mode("f", m.mode),
                                          tar.extractfile(m).read())
            else:
                continue  # devices, fifos: refused by the data filter
            n += 1
    return n


def upload_tar(entries: dict[str, Entry]) -> bytes:
    """Tar for put_archive('/') of a set of entries, parents included, so
    the daemon creates intermediate dirs root-owned 0755."""
    b = Bundle(dict(entries))
    for p in list(entries):
        d = posixpath.dirname(p)
        while d not in ("/", ""):
            b.entries.setdefault(d, Entry("d", 0o755))
            d = posixpath.dirname(d)
    return b.to_tar()


def from_harbor_trial(trial_dir: str, declared: list[str]) -> Bundle:
    """Bundle from a recorded Harbor trial directory (`harbor trial download`
    / a local `harbor run`): artifacts live at <trial>/artifacts/<path without
    leading slash>."""
    root = os.path.join(trial_dir, "artifacts")
    b = Bundle(declared=list(declared))
    for dirpath, dirnames, filenames in os.walk(root):
        for name in dirnames + filenames:
            hp = os.path.join(dirpath, name)
            p = "/" + os.path.relpath(hp, root).replace(os.sep, "/")
            if not any(under(p, d) for d in declared):
                continue
            st = os.lstat(hp)
            if stat.S_ISLNK(st.st_mode):
                b.entries[p] = Entry("l", 0o777, target=os.readlink(hp))
            elif stat.S_ISDIR(st.st_mode):
                b.entries[p] = Entry("d", 0o755)
            elif stat.S_ISREG(st.st_mode):
                with open(hp, "rb") as f:
                    b.entries[p] = Entry("f", _data_filter_mode("f", st.st_mode), f.read())
    return b


def to_harbor_trial(bundle: Bundle, source_trial: str, dest: str) -> str:
    """Copy a recorded Harbor trial and swap its artifacts for this bundle's
    bytes, so `harbor trial regrade` judges exactly what Breakwater judged."""
    import json
    import shutil
    shutil.copytree(source_trial, dest, symlinks=True)
    art = os.path.join(dest, "artifacts")
    for d in bundle.declared:
        tgt = os.path.join(art, d.strip("/"))
        if os.path.isdir(tgt) and not os.path.islink(tgt):
            shutil.rmtree(tgt)
        elif os.path.lexists(tgt):
            os.remove(tgt)
    for p, e in sorted(bundle.entries.items()):
        hp = os.path.join(art, p.lstrip("/"))
        if e.kind == "d":
            os.makedirs(hp, exist_ok=True)
        elif e.kind == "l":
            os.makedirs(os.path.dirname(hp), exist_ok=True)
            os.symlink(e.target, hp)
        else:
            os.makedirs(os.path.dirname(hp), exist_ok=True)
            with open(hp, "wb") as f:
                f.write(e.data)
            os.chmod(hp, e.mode)
    for fn in ("result.json",):           # the regrade must not read the old verdict
        p = os.path.join(dest, fn)
        if os.path.exists(p):
            r = json.load(open(p))
            r["verifier_result"] = None
            json.dump(r, open(p, "w"))
    return dest
