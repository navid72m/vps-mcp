"""Download a commit archive on the laptop and rebuild it as a clean tar (research G9).

Whatever the repository contains, the tar that reaches the VPS only has regular files,
directories and in-tree relative symlinks, with normalised modes and root ownership.
"""

from __future__ import annotations

import contextlib
import hashlib
import io
import json
import os
import posixpath
import re
import tarfile
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

from vps_mcp.errors import ErrorCategory, VpsMcpError
from vps_mcp.gitlab.client import GitLabClient

MANIFEST = "MANIFEST.sha256"
MARKER = ".vps-mcp-release"
EXPANSION_FACTOR = 4


@dataclass
class Clean:
    tar_bytes: bytes
    files: list[str]
    skipped: list[tuple[str, str]] = field(default_factory=list)
    total_bytes: int = 0


def glob_re(pattern: str) -> re.Pattern[str]:
    """gitignore-like: no '/' matches the basename anywhere; '**' spans directories."""
    anchored = "/" in pattern.rstrip("/")
    out = ""
    i = 0
    while i < len(pattern):
        c = pattern[i]
        if pattern.startswith("**/", i):
            out += "(?:.*/)?"
            i += 3
            continue
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
            continue
        out += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
        i += 1
    return re.compile(("^" if anchored else r"^(?:.*/)?") + out + r"(?:/.*)?$")


def selected(rel: str, include: list[str], exclude: list[str]) -> bool:
    if include and not any(glob_re(p).match(rel) for p in include):
        return False
    return not any(glob_re(p).match(rel) for p in exclude)


async def download(client: GitLabClient, project: str, sha: str, cap_bytes: int) -> Path:
    """Stream the archive into a private temp file; abort past ``cap_bytes``."""
    tmpdir = Path(tempfile.mkdtemp(prefix="vps-mcp-archive-"))
    os.chmod(tmpdir, 0o700)
    dest = tmpdir / "archive.tar.gz"
    size = 0
    try:
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as fh:
            async with client.stream(
                f"/projects/{project}/repository/archive.tar.gz", {"sha": sha}
            ) as resp:
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > cap_bytes:
                        raise VpsMcpError(
                            ErrorCategory.INVALID_INPUT,
                            f"the archive is larger than max_archive_mb ({cap_bytes >> 20} MB)",
                        )
                    fh.write(chunk)
    except BaseException:
        cleanup(dest)
        raise
    return dest


def cleanup(path: Path) -> None:
    with contextlib.suppress(OSError):
        path.unlink()
    with contextlib.suppress(OSError):
        path.parent.rmdir()


def _strip_top(name: str) -> str | None:
    parts = name.split("/", 1)
    return parts[1] if len(parts) == 2 and parts[1] else None


def _safe_rel(rel: str) -> bool:
    norm = posixpath.normpath(rel)
    return not (
        rel.startswith("/") or norm.startswith("..") or norm == "." or "\x00" in rel
    ) and norm == rel.rstrip("/")


def sanitize(
    src: Path,
    *,
    include: list[str],
    exclude: list[str],
    cap_bytes: int,
    release_dir: str,
    marker: dict[str, str],
) -> Clean:
    """Rebuild ``src`` (a GitLab archive) as a clean tar for ``release_dir``."""
    limit = cap_bytes * EXPANSION_FACTOR
    out = io.BytesIO()
    files: list[str] = []
    skipped: list[tuple[str, str]] = []
    digests: list[str] = []
    total = 0
    dirs: set[str] = set()
    try:
        tin = tarfile.open(src, mode="r:gz")
    except (tarfile.TarError, OSError) as exc:
        raise VpsMcpError(ErrorCategory.FAILED, f"unreadable archive: {exc}") from None
    with tin, tarfile.open(fileobj=out, mode="w", format=tarfile.PAX_FORMAT) as tout:
        for m in tin:
            rel = _strip_top(m.name)
            if rel is None:
                continue
            if not _safe_rel(rel):
                skipped.append((m.name, "unsafe path"))
                continue
            if rel in (MANIFEST, MARKER):
                skipped.append((rel, "reserved name"))
                continue
            if m.isdir():
                continue
            if not selected(rel, include, exclude):
                continue
            if m.issym():
                target = m.linkname
                resolved = posixpath.normpath(posixpath.join(posixpath.dirname(rel), target))
                if target.startswith("/") or resolved.startswith(".."):
                    skipped.append((rel, "symlink leaves the tree"))
                    continue
                info = tarfile.TarInfo(rel)
                info.type = tarfile.SYMTYPE
                info.linkname = target
                info.mode = 0o777
            elif m.isfile():
                total += m.size
                if total > limit:
                    raise VpsMcpError(
                        ErrorCategory.INVALID_INPUT,
                        "the archive expands beyond the allowed size (max_archive_mb x 4)",
                    )
                fh = tin.extractfile(m)
                data = fh.read() if fh else b""
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                info.mode = 0o755 if m.mode & 0o111 else 0o644
                digests.append(f"{hashlib.sha256(data).hexdigest()}  {release_dir}/{rel}")
                files.append(rel)
            else:
                skipped.append((rel, "hard link or special file"))
                continue
            parent = posixpath.dirname(rel)
            while parent and parent not in dirs:
                dirs.add(parent)
                parent = posixpath.dirname(parent)
            info.uid = info.gid = 0
            info.uname = info.gname = ""
            info.mtime = int(m.mtime)
            if info.isfile():
                tout.addfile(info, io.BytesIO(data))
            else:
                tout.addfile(info)
        for name, payload in (
            (MANIFEST, "\n".join(digests) + "\n"),
            (MARKER, json.dumps(marker, sort_keys=True) + "\n"),
        ):
            raw = payload.encode()
            info = tarfile.TarInfo(name)
            info.size, info.mode, info.uid, info.gid = len(raw), 0o644, 0, 0
            tout.addfile(info, io.BytesIO(raw))
    return Clean(out.getvalue(), files, skipped, total)
