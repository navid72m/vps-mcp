"""Approved-source checks for container images (FR-014)."""

from __future__ import annotations

from vps_mcp.errors import ErrorCategory, VpsMcpError


def normalize_image(ref: str) -> str:
    """Expand short references the way Docker does: redis:7 -> docker.io/library/redis:7."""
    first, sep, rest = ref.partition("/")
    if not sep:
        return f"docker.io/library/{ref}"
    if "." in first or ":" in first or first == "localhost":
        if first in ("index.docker.io", "registry-1.docker.io"):
            return f"docker.io/{rest}"
        return ref
    return f"docker.io/{ref}"


def normalize_prefix(prefix: str) -> str:
    p = prefix.strip()
    if p and "/" not in p.rstrip("/"):
        # a bare registry host like "ghcr.io" means everything on it
        return p.rstrip("/") + "/"
    return p


def check_image_approved(image: str, prefixes: list[str]) -> str:
    full = normalize_image(image)
    for p in prefixes:
        np = normalize_prefix(p)
        if np and (full.startswith(np) or full == np.rstrip("/")):
            return full
    raise VpsMcpError(
        ErrorCategory.UNAPPROVED_SOURCE,
        f"image '{image}' (resolved to '{full}') does not match any approved image_prefixes",
    )
