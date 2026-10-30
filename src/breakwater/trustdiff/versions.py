"""Task versions: a task directory pinned to a git commit (or a plain dir).

`git archive` exports exactly the committed tree, so a version is immutable
and reproducible from (repo URL, commit, task name); exports are cached under
$BW_HOME/tasks/<commit>/<task>.
"""
from __future__ import annotations

import io
import os
import subprocess
import tarfile
from dataclasses import dataclass

from .. import cas
from ..adapters.harbor_task import parse_task


def _git(repo: str, *args: str) -> str:
    return subprocess.run(["git", "-C", repo, *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@dataclass
class TaskVersion:
    label: str            # "base" | "head" | free text
    task: str             # task directory name
    path: str             # materialized task dir
    commit: str | None    # git commit, None for a plain directory
    repo_url: str | None

    @property
    def spec(self):
        return parse_task(self.path)

    def describe(self) -> str:
        return f"{self.label}@{self.commit[:10]}" if self.commit else f"{self.label}:{self.path}"


class Source:
    """Where task versions come from: a git ref in a repo, or a tasks dir."""

    def __init__(self, label: str, *, repo: str | None = None, ref: str | None = None,
                 tasks_dir: str | None = None, subdir: str = "tasks"):
        self.label, self.repo, self.ref, self.subdir = label, repo, ref, subdir
        self.tasks_dir = tasks_dir
        self.commit = _git(repo, "rev-parse", f"{ref}^{{commit}}") if repo else None
        self.repo_url = None
        if repo:
            try:
                self.repo_url = _git(repo, "config", "--get", "remote.origin.url")
            except subprocess.CalledProcessError:
                pass

    def __repr__(self):
        return f"{self.label}={self.ref or self.tasks_dir}"

    def list_tasks(self) -> list[str]:
        if self.tasks_dir:
            return sorted(d for d in os.listdir(self.tasks_dir)
                          if os.path.isfile(os.path.join(self.tasks_dir, d, "task.toml")))
        out = _git(self.repo, "ls-tree", "--name-only", f"{self.commit}:{self.subdir}")
        return sorted(n for n in out.splitlines() if not n.endswith((".md", ".toml")))

    def version(self, task: str) -> TaskVersion | None:
        if self.tasks_dir:
            p = os.path.join(self.tasks_dir, task)
            return TaskVersion(self.label, task, p, None, None) if os.path.isdir(p) else None
        dest = os.path.join(cas.BW_HOME, "tasks", self.commit[:16], task)
        if not os.path.isfile(os.path.join(dest, "task.toml")):
            try:
                raw = subprocess.run(
                    ["git", "-C", self.repo, "archive", "--format=tar", self.commit,
                     f"{self.subdir}/{task}"], check=True, capture_output=True).stdout
            except subprocess.CalledProcessError:
                return None
            tmp = dest + ".partial"
            os.makedirs(tmp, exist_ok=True)
            prefix = f"{self.subdir}/{task}/"
            with tarfile.open(fileobj=io.BytesIO(raw)) as tar:
                for m in tar.getmembers():
                    if not m.name.startswith(prefix):
                        continue
                    m.name = m.name[len(prefix):]
                    if m.name:
                        tar.extract(m, tmp, filter="tar")
            if os.path.exists(dest):          # a concurrent export won the race
                import shutil
                shutil.rmtree(tmp, ignore_errors=True)
            else:
                os.replace(tmp, dest)
        return TaskVersion(self.label, task, dest, self.commit, self.repo_url)


def changed_verifiers(base: Source, head: Source, tasks: list[str] | None = None) -> list[str]:
    """Tasks present in both sources whose verifier digest differs."""
    names = tasks or sorted(set(base.list_tasks()) & set(head.list_tasks()))
    out = []
    for t in names:
        b, h = base.version(t), head.version(t)
        if b and h and b.spec.digests["verifier"] != h.spec.digests["verifier"]:
            out.append(t)
    return out
