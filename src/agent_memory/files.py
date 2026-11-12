"""Store layout, safe paths, atomic writes, archive, locking (research A2, A15)."""

from __future__ import annotations

import contextlib
import errno
import fcntl
import os
import stat
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from agent_memory.errors import AgentMemoryError, invalid
from agent_memory.model import file_name, scope_dir

README = """\
# agent-memory store

Persistent memory shared by every agent that connects to agent-memory (or vps-mcp).

- `memories/<scope>/` - one Markdown file per memory (front matter + content). Safe to read,
  edit or delete by hand; the index notices on the next search.
- `archive/`          - archived memories (move one back into memories/ to restore it).
- `events/`           - append-only history and audit (no memory content).
- `index.sqlite`      - search index; rebuilt automatically from the files, safe to delete.

Keep this folder private. If you version it with git, use a private repository.
"""


def default_store() -> Path:
    env = os.environ.get("AGENT_MEMORY_HOME")
    if env:
        return Path(env).expanduser()
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base).expanduser() / "agent-memory"


@dataclass
class Malformed:
    path: str
    reason: str


class Files:
    def __init__(self, root: Path) -> None:
        self.root = Path(root).expanduser()
        self.memories = self.root / "memories"
        self.archive_dir = self.root / "archive"
        self.events = self.root / "events"

    def ensure(self) -> None:
        for d in (self.root, self.memories, self.archive_dir, self.events):
            if d.is_symlink():
                raise AgentMemoryError("failed", f"store component is a symlink: {d.name}")
            d.mkdir(mode=0o700, parents=True, exist_ok=True)
            with contextlib.suppress(OSError):
                os.chmod(d, 0o700)
        readme = self.root / "README.md"
        if not readme.exists():
            self.write_atomic(readme, README)

    # ------------------------------------------------------------------ paths

    def rel(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def abs(self, rel: str) -> Path:
        p = self.root / rel
        resolved = p.resolve()
        if self.root.resolve() not in resolved.parents:
            raise invalid("path escapes the memory store")
        return p

    def new_path(self, scope: str, subject: str, mem_id: str) -> Path:
        d = self.memories / scope_dir(scope)
        p = d / file_name(subject, mem_id)
        if self.memories.resolve() not in p.resolve().parents:
            raise invalid("path escapes the memory store")
        return p

    # -------------------------------------------------------------------- I/O

    @staticmethod
    def read(path: Path | str) -> str:
        fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise OSError(errno.EINVAL, "not a regular file")
            if st.st_size > 256 * 1024:
                raise OSError(errno.EFBIG, "file too large")
            with os.fdopen(fd, "rb", closefd=False) as fh:
                return fh.read().decode("utf-8")
        finally:
            os.close(fd)

    def write_atomic(self, path: Path, text: str) -> None:
        if path.is_symlink():
            raise AgentMemoryError("failed", f"refusing to write through a symlink: {path.name}")
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        for parent in path.parents:
            if parent == self.root:
                break
            if parent.is_symlink():
                raise AgentMemoryError("failed", "refusing to write through a symlinked folder")
        tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
            with os.fdopen(fd, "wb") as fh:
                fh.write(text.encode("utf-8"))
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, path)
        except OSError as exc:
            with contextlib.suppress(OSError):
                tmp.unlink()
            raise AgentMemoryError("failed", f"could not write {path.name}: {exc}") from None

    def iter_files(self) -> Iterator[tuple[str, str, os.stat_result]]:
        """Regular ``*.md`` files under memories/, never following symlinks.

        Yields ``(relative path, absolute path, stat)``; plain strings keep this fast enough
        to run before every search (thousands of files).
        """
        if not self.memories.is_dir() or self.memories.is_symlink():
            return
        root = str(self.root)
        cut = len(root) + 1
        stack = [str(self.memories)]
        while stack:
            d = stack.pop()
            try:
                with os.scandir(d) as it:
                    entries = list(it)
            except OSError:
                continue
            for e in entries:
                name = e.name
                if name.startswith(".") or e.is_symlink():
                    continue
                if e.is_dir(follow_symlinks=False):
                    stack.append(e.path)
                elif name.endswith(".md") and e.is_file(follow_symlinks=False):
                    yield e.path[cut:], e.path, e.stat(follow_symlinks=False)

    def symlinks(self) -> list[str]:
        out = []
        for dirpath, dirnames, filenames in os.walk(self.memories, followlinks=False):
            for n in dirnames + filenames:
                p = Path(dirpath) / n
                if p.is_symlink():
                    out.append(self.rel(p))
        return out

    def archive(self, rel: str) -> str:
        src = self.abs(rel)
        dest = self.archive_dir / Path(rel).relative_to("memories")
        dest.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.replace(src, dest)
        return self.rel(dest)

    @contextlib.contextmanager
    def lock(self) -> Iterator[None]:
        self.ensure()
        fd = os.open(self.root / ".lock", os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)
