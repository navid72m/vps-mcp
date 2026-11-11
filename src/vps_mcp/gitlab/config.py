"""`[gitlab]` configuration (specs/005-gitlab-integration, data-model.md §1)."""

from __future__ import annotations

import posixpath
import re
from typing import Annotated, Literal
from urllib.parse import urlsplit

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
PROJECT_PATH_RE = re.compile(r"^[A-Za-z0-9._-]+(/[A-Za-z0-9._-]+)+$")
KEYCHAIN_SERVICE_RE = re.compile(r"^[A-Za-z0-9._-]{1,128}$")
KEYCHAIN_ACCOUNT_RE = re.compile(r"^[A-Za-z0-9._@-]{1,128}$")
REF_GLOB_RE = re.compile(r"^(?:(?:branch|tag):)?[A-Za-z0-9._/*?\[\]-]{1,200}$")
PATH_GLOB_RE = re.compile(r"^[A-Za-z0-9._/*?\[\]{},-]{1,200}$")
_USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")

PERMISSIONS = ("read", "write_issues", "write_mrs", "ci", "deploy")
Permission = Literal["read", "write_issues", "write_mrs", "ci", "deploy"]
FORBIDDEN_DESTINATIONS = (
    "/etc",
    "/bin",
    "/sbin",
    "/usr",
    "/lib",
    "/lib32",
    "/lib64",
    "/libx32",
    "/boot",
    "/dev",
    "/proc",
    "/sys",
    "/run",
    "/var/lib/vps-mcp",
)
DEPLOY_ROOT = "/var/lib/vps-mcp/deploys"


def _slug(v: str) -> str:
    if not SLUG_RE.fullmatch(v):
        raise ValueError("must match ^[a-z0-9][a-z0-9-]{0,39}$")
    return v


def _https_url(v: str) -> str:
    parts = urlsplit(v)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError("gitlab.url must be an https:// URL")
    if parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("gitlab.url must not contain credentials, a query or a fragment")
    return v.rstrip("/")


def _http_url(v: str) -> str:
    parts = urlsplit(v)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError("health_url must be an http(s) URL")
    if parts.username or parts.password:
        raise ValueError("health_url must not contain credentials")
    if any(c in v for c in " \t\n'\"`$\\"):
        raise ValueError("health_url contains invalid characters")
    return v


def _abs_path(v: str) -> str:
    if not v.startswith("/") or "\x00" in v or "\n" in v:
        raise ValueError("must be an absolute path")
    norm = posixpath.normpath(v)
    if norm != v.rstrip("/") or ".." in v.split("/"):
        raise ValueError("must be a normalised absolute path (no '..', '//' or trailing '/')")
    if not re.fullmatch(r"^/[A-Za-z0-9._/@+-]{1,255}$", norm):
        raise ValueError("contains unsupported characters")
    return norm


def _destination(v: str) -> str:
    v = _abs_path(v)
    if v == "/" or v.count("/") < 2:
        raise ValueError("destination must be at least two levels deep (e.g. /opt/app)")
    for bad in FORBIDDEN_DESTINATIONS:
        if v == bad or v.startswith(bad + "/"):
            raise ValueError(f"destination must not be under {bad}")
    return v


def _unix_user(v: str) -> str:
    if not _USER_RE.fullmatch(v):
        raise ValueError("must be a valid user or group name")
    return v


def _ref_glob(v: str) -> str:
    if not REF_GLOB_RE.fullmatch(v) or ".." in v:
        raise ValueError("ref patterns look like 'main', 'release/*', 'tag:v*' or 'branch:main'")
    return v


def _path_glob(v: str) -> str:
    if not PATH_GLOB_RE.fullmatch(v) or v.startswith("/") or ".." in v.split("/"):
        raise ValueError("include/exclude entries must be relative globs without '..'")
    return v


Slug = Annotated[str, AfterValidator(_slug)]
RefGlob = Annotated[str, AfterValidator(_ref_glob)]
PathGlob = Annotated[str, AfterValidator(_path_glob)]
UnixUser = Annotated[str, AfterValidator(_unix_user)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProjectDecl(_Strict):
    alias: Slug
    id: int | None = Field(default=None, ge=1)
    path: str | None = None
    permissions: list[Permission] = Field(default_factory=lambda: list[Permission](["read"]))
    close_issues: bool = False
    close_any_issue: bool = False
    trigger_refs: list[RefGlob] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _rules(self) -> ProjectDecl:
        if (self.id is None) == (self.path is None):
            raise ValueError(f"gitlab project {self.alias}: set exactly one of 'id' or 'path'")
        if self.path is not None and not PROJECT_PATH_RE.fullmatch(self.path):
            raise ValueError(f"gitlab project {self.alias}: path must look like 'group/project'")
        perms: list[Permission] = list(dict.fromkeys(["read", *self.permissions]))
        self.permissions = perms
        if self.close_issues and "write_issues" not in perms:
            raise ValueError(f"gitlab project {self.alias}: close_issues requires write_issues")
        if self.close_any_issue and not self.close_issues:
            raise ValueError(f"gitlab project {self.alias}: close_any_issue requires close_issues")
        if self.trigger_refs and "ci" not in perms:
            raise ValueError(f"gitlab project {self.alias}: trigger_refs requires 'ci'")
        return self

    def allows(self, permission: str) -> bool:
        return permission in self.permissions


class DeployTarget(_Strict):
    name: Slug
    project: Slug
    refs: list[RefGlob] = Field(min_length=1, max_length=20)
    destination: Annotated[str, AfterValidator(_destination)]
    owner: UnixUser = "root"
    group: UnixUser = "root"
    include: list[PathGlob] = Field(default_factory=list, max_length=50)
    exclude: list[PathGlob] = Field(default_factory=list, max_length=50)
    services: list[str] = Field(default_factory=list, max_length=10)
    health_url: Annotated[str, AfterValidator(_http_url)] | None = None
    require_pipeline_success: bool = False
    keep_releases: int = Field(default=3, ge=1, le=20)

    @property
    def root(self) -> str:
        return f"{DEPLOY_ROOT}/{self.name}"

    @property
    def releases_dir(self) -> str:
        return f"{self.root}/releases"


class GitLabSettings(_Strict):
    url: Annotated[str, AfterValidator(_https_url)]
    keychain_service: str = "vps-mcp-gitlab"
    keychain_account: str
    ca_bundle: Annotated[str, AfterValidator(_abs_path)] | None = None
    timeout_s: int = Field(default=30, ge=5, le=120)
    max_archive_mb: int = Field(default=200, ge=1, le=2000)
    project: list[ProjectDecl] = Field(default_factory=list, max_length=50)
    deploy: list[DeployTarget] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _rules(self) -> GitLabSettings:
        if not KEYCHAIN_SERVICE_RE.fullmatch(self.keychain_service):
            raise ValueError("gitlab.keychain_service must match [A-Za-z0-9._-]{1,128}")
        if not KEYCHAIN_ACCOUNT_RE.fullmatch(self.keychain_account):
            raise ValueError("gitlab.keychain_account must match [A-Za-z0-9._@-]{1,128}")
        aliases = [p.alias for p in self.project]
        if len(set(aliases)) != len(aliases):
            raise ValueError("gitlab project aliases must be unique")
        names = [d.name for d in self.deploy]
        if len(set(names)) != len(names):
            raise ValueError("gitlab deploy target names must be unique")
        by_alias = {p.alias: p for p in self.project}
        dests: list[str] = []
        for d in self.deploy:
            proj = by_alias.get(d.project)
            if proj is None:
                raise ValueError(f"deploy {d.name}: project '{d.project}' is not declared")
            if not proj.allows("deploy"):
                raise ValueError(f"deploy {d.name}: project '{d.project}' lacks 'deploy'")
            for other in dests:
                if (
                    d.destination == other
                    or d.destination.startswith(other + "/")
                    or (other.startswith(d.destination + "/"))
                ):
                    raise ValueError(f"deploy {d.name}: destination overlaps another target")
            dests.append(d.destination)
        return self

    def get_project(self, alias: str) -> ProjectDecl | None:
        return next((p for p in self.project if p.alias == alias), None)

    def get_target(self, name: str) -> DeployTarget | None:
        return next((d for d in self.deploy if d.name == name), None)
