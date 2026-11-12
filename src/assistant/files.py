"""File system operations on the local machine (files__* tools)."""

from __future__ import annotations

import fnmatch
import os
import stat
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

MAX_READ_BYTES = 50 * 1024 * 1024  # 50 MB safety cap
DEFAULT_SLICE_LINES = 500
MAX_LIST_ENTRIES = 150
MAX_SEARCH_RESULTS = 50
IGNORED_DIRS = {
    ".git",
    "__pycache__",
    "node_modules",
    ".venv",
    ".pytest_cache",
    ".DS_Store",
    ".Trash",
}


def resolve_path(raw_path: str) -> Path:
    """Resolve ~, environment variables, and relative paths safely."""
    expanded = os.path.expanduser(os.path.expandvars(raw_path.strip()))
    p = Path(expanded)
    if not p.is_absolute():
        p = Path.home() / p
    return p.resolve()


def files_read(
    path: str,
    start_line: int = 1,
    end_line: int = DEFAULT_SLICE_LINES,
) -> dict[str, Any]:
    """Read a text file with 1-based start_line and end_line bounds."""
    try:
        target = resolve_path(path)
        if not target.exists():
            return {"ok": False, "error": f"File not found: {target}"}
        if target.is_dir():
            return {"ok": False, "error": f"Target is a directory, not a file: {target}"}

        size = target.stat().st_size
        if size > MAX_READ_BYTES:
            return {
                "ok": False,
                "error": f"File is too large ({size / (1024 * 1024):.1f} MB, max {MAX_READ_BYTES // (1024 * 1024)} MB): {target}",
            }

        try:
            content_str = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            # Check for binary file
            return {
                "ok": True,
                "path": str(target),
                "size_bytes": size,
                "is_binary": True,
                "message": "Binary file (non-UTF-8); content cannot be displayed as text.",
            }

        lines = content_str.splitlines()
        total_lines = len(lines)

        s_line = max(1, start_line)
        e_line = min(total_lines, max(s_line, end_line))

        if total_lines == 0:
            formatted = "(empty file)"
        else:
            selected = lines[s_line - 1 : e_line]
            width = len(str(e_line))
            formatted = "\n".join(
                f"{i + s_line:>{width}} | {line}" for i, line in enumerate(selected)
            )

        return {
            "ok": True,
            "path": str(target),
            "total_lines": total_lines,
            "start_line": s_line,
            "end_line": e_line,
            "size_bytes": size,
            "content": formatted,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def files_write(path: str, content: str, overwrite: bool = False) -> dict[str, Any]:
    """Write text content to a file. Fails if file exists unless overwrite is True."""
    try:
        target = resolve_path(path)
        if target.exists() and not overwrite:
            return {
                "ok": False,
                "error": f"File already exists: {target}. Set overwrite=True to replace it.",
            }

        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        size = target.stat().st_size
        return {
            "ok": True,
            "path": str(target),
            "bytes_written": size,
            "lines": len(content.splitlines()),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def files_edit(path: str, target: str, replacement: str) -> dict[str, Any]:
    """Replace occurrences of `target` with `replacement` in a text file."""
    try:
        target_path = resolve_path(path)
        if not target_path.exists():
            return {"ok": False, "error": f"File not found: {target_path}"}
        if target_path.is_dir():
            return {"ok": False, "error": f"Target is a directory: {target_path}"}

        text = target_path.read_text(encoding="utf-8")
        if target not in text:
            return {"ok": False, "error": f"Target string not found in {target_path}"}

        count = text.count(target)
        new_text = text.replace(target, replacement)
        target_path.write_text(new_text, encoding="utf-8")
        return {
            "ok": True,
            "path": str(target_path),
            "replacements": count,
            "bytes_written": len(new_text.encode("utf-8")),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def files_list(
    path: str = "~",
    max_depth: int = 1,
    pattern: str = "*",
    include_hidden: bool = False,
) -> dict[str, Any]:
    """List entries in a directory up to max_depth."""
    try:
        base = resolve_path(path)
        if not base.exists():
            return {"ok": False, "error": f"Directory not found: {base}"}
        if not base.is_dir():
            return {"ok": False, "error": f"Path is not a directory: {base}"}

        entries: list[dict[str, Any]] = []
        depth_limit = max(1, min(3, max_depth))

        def walk(current: Path, depth: int) -> None:
            if depth > depth_limit or len(entries) >= MAX_LIST_ENTRIES:
                return
            try:
                items = sorted(current.iterdir(), key=lambda p: (not p.is_dir(), p.name.lower()))
            except PermissionError:
                return
            for item in items:
                if len(entries) >= MAX_LIST_ENTRIES:
                    break
                if not include_hidden and item.name.startswith("."):
                    continue
                if item.is_dir() and item.name in IGNORED_DIRS:
                    continue

                if fnmatch.fnmatch(item.name, pattern):
                    kind = "directory" if item.is_dir() else "file"
                    if item.is_symlink():
                        kind = "symlink"
                    try:
                        st = item.stat()
                        size = st.st_size if kind == "file" else 0
                        mtime = datetime.fromtimestamp(st.st_mtime, UTC).isoformat()
                    except OSError:
                        size, mtime = 0, ""

                    entries.append(
                        {
                            "name": item.name,
                            "path": str(item),
                            "type": kind,
                            "size_bytes": size,
                            "modified": mtime,
                        }
                    )

                if item.is_dir() and depth < depth_limit:
                    walk(item, depth + 1)

        walk(base, 1)
        return {
            "ok": True,
            "path": str(base),
            "count": len(entries),
            "truncated": len(entries) >= MAX_LIST_ENTRIES,
            "entries": entries,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def files_search(
    path: str = "~",
    query: str = "",
    pattern: str = "*",
    max_results: int = 25,
) -> dict[str, Any]:
    """Search for files by name glob and/or search inside files for text content."""
    try:
        base = resolve_path(path)
        if not base.exists():
            return {"ok": False, "error": f"Directory not found: {base}"}

        limit = max(1, min(MAX_SEARCH_RESULTS, max_results))
        matches: list[dict[str, Any]] = []

        query_lower = query.lower() if query else ""

        for root, dirs, files in os.walk(base):
            # Prune ignored dirs in-place
            dirs[:] = [d for d in dirs if not d.startswith(".") and d not in IGNORED_DIRS]

            for fname in files:
                if len(matches) >= limit:
                    break
                if fname.startswith("."):
                    continue
                if not fnmatch.fnmatch(fname, pattern):
                    continue

                fpath = Path(root) / fname
                if not query_lower:
                    matches.append({"path": str(fpath), "type": "filename_match"})
                    continue

                # Search within content
                try:
                    if fpath.stat().st_size > 5 * 1024 * 1024:  # skip files >5MB for grepping
                        continue
                    text = fpath.read_text(encoding="utf-8", errors="ignore")
                    for line_idx, line in enumerate(text.splitlines(), start=1):
                        if query_lower in line.lower():
                            matches.append(
                                {
                                    "path": str(fpath),
                                    "line": line_idx,
                                    "snippet": line.strip()[:200],
                                }
                            )
                            if len(matches) >= limit:
                                break
                except (OSError, UnicodeDecodeError):
                    continue

            if len(matches) >= limit:
                break

        return {
            "ok": True,
            "path": str(base),
            "pattern": pattern,
            "query": query,
            "count": len(matches),
            "matches": matches,
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def files_info(path: str) -> dict[str, Any]:
    """Get metadata for a file or directory."""
    try:
        target = resolve_path(path)
        if not target.exists():
            return {"ok": False, "exists": False, "path": str(target)}

        st = target.stat()
        return {
            "ok": True,
            "exists": True,
            "path": str(target),
            "name": target.name,
            "is_file": target.is_file(),
            "is_dir": target.is_dir(),
            "is_symlink": target.is_symlink(),
            "size_bytes": st.st_size,
            "modified": datetime.fromtimestamp(st.st_mtime, UTC).isoformat(),
            "created": datetime.fromtimestamp(st.st_ctime, UTC).isoformat(),
            "mode": oct(stat.S_IMODE(st.st_mode)),
        }
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}


def files_delete(path: str) -> dict[str, Any]:
    """Delete a file or empty directory."""
    try:
        target = resolve_path(path)
        if not target.exists():
            return {"ok": False, "error": f"Path not found: {target}"}

        if target.is_dir():
            target.rmdir()  # fails with OSError if directory is not empty
        else:
            target.unlink()

        return {"ok": True, "path": str(target), "deleted": True}
    except OSError as exc:
        return {"ok": False, "error": str(exc)}
