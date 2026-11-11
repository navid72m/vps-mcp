"""Policy and connection configuration (contracts/policy-config.md, data-model.md §1)."""

from __future__ import annotations

import os
import re
from enum import StrEnum
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from vps_mcp.migration.config import MigrationSettings
from vps_mcp.models.inputs import AbsPath, PackageName, ServiceKind, ServiceName

VALIDATOR_KEYS = ("nginx", "sshd", "caddy", "haproxy")
# A dot alone does not make a unit suffix: "php8.1-fpm" is "php8.1-fpm.service".
UNIT_SUFFIXES = (".service", ".socket", ".timer", ".target", ".mount", ".path", ".slice", ".scope")


def _vps_name(v: str) -> str:
    if not re.fullmatch(r"^[a-z0-9-]{1,32}$", v):
        raise ValueError("vps.name must match ^[a-z0-9-]{1,32}$")
    return v


def _host(v: str) -> str:
    if not re.fullmatch(r"^[A-Za-z0-9.:_-]{1,253}$", v):
        raise ValueError("vps.host must be a hostname or IP address")
    return v


def _unix_user(v: str) -> str:
    if not re.fullmatch(r"^[a-z_][a-z0-9_-]{0,31}$", v):
        raise ValueError("must be a valid user or group name")
    return v


def _mode(v: str) -> str:
    if not re.fullmatch(r"^0?[0-7]{3,4}$", v):
        raise ValueError("mode must be an octal string like '0644'")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ContainerRuntime(StrEnum):
    DOCKER = "docker"
    PODMAN = "podman"
    NONE = "none"


class PackageManager(StrEnum):
    APT = "apt"
    DNF = "dnf"


class TargetVPS(_Strict):
    name: Annotated[str, AfterValidator(_vps_name)]
    host: Annotated[str, AfterValidator(_host)]
    port: int = Field(default=22, ge=1, le=65535)
    user: Annotated[str, AfterValidator(_unix_user)] = "vpsmcp"
    key_path: str | None = None
    key_passphrase_env: str | None = None  # name of an env var holding the key passphrase
    known_hosts_path: str
    container_runtime: ContainerRuntime = ContainerRuntime.DOCKER
    package_manager: PackageManager = PackageManager.APT
    allow_root: bool = False

    @model_validator(mode="after")
    def _root_needs_opt_in(self) -> TargetVPS:
        if self.user == "root" and not self.allow_root:
            raise ValueError("vps.user = 'root' requires vps.allow_root = true")
        return self


class Timeouts(_Strict):
    control_s: int = Field(default=60, ge=1, le=3600)
    deploy_s: int = Field(default=600, ge=1, le=7200)
    query_s: int = Field(default=15, ge=1, le=600)


class ApprovedSources(_Strict):
    apt_origins: list[str] = Field(default_factory=list)
    dnf_repos: list[str] = Field(default_factory=list)
    image_prefixes: list[str] = Field(default_factory=list)


class Policy(_Strict):
    read_only: bool = False
    timeouts: Timeouts = Field(default_factory=Timeouts)
    health_stabilize_s: int = Field(default=10, ge=0, le=600)
    log_default_lines: int = Field(default=200, ge=1, le=5000)
    log_max_lines: int = Field(default=5000, ge=1, le=5000)
    min_free_disk_mb: int = Field(default=1024, ge=0)
    audit_retention_days: int = Field(default=90, ge=1)
    revisions_kept: int = Field(default=5, ge=2, le=100)
    secret_keys: list[str] = Field(
        default_factory=lambda: ["*PASSWORD*", "*SECRET*", "*TOKEN*", "*KEY*"]
    )
    approved_sources: ApprovedSources = Field(default_factory=ApprovedSources)
    remote_access_units: list[str] = Field(default_factory=lambda: ["ssh", "sshd"])


class ConfigFileDecl(_Strict):
    path: AbsPath
    owner: Annotated[str, AfterValidator(_unix_user)] = "root"
    group: Annotated[str, AfterValidator(_unix_user)] = "root"
    mode: Annotated[str, AfterValidator(_mode)] = "0644"


class ServiceDeclaration(_Strict):
    name: ServiceName
    kind: ServiceKind
    unit: ServiceName | None = None
    description: str | None = Field(default=None, max_length=200)
    protected: bool = False
    config_files: list[ConfigFileDecl] = Field(default_factory=list)
    validate_with: Literal["nginx", "sshd", "caddy", "haproxy"] | None = Field(
        default=None, alias="validate"
    )
    activate: Literal["reload", "restart"] = "restart"
    data_paths: list[AbsPath] = Field(default_factory=list)
    package: PackageName | None = None

    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    @property
    def unit_name(self) -> str:
        unit = self.unit or self.name
        return unit if unit.endswith(UNIT_SUFFIXES) else f"{unit}.service"

    @property
    def key(self) -> tuple[str, str]:
        return (self.name, self.kind.value)

    @model_validator(mode="after")
    def _os_only_fields(self) -> ServiceDeclaration:
        if self.kind is ServiceKind.CONTAINER:
            if self.config_files or self.validate_with or self.unit or self.package:
                raise ValueError(
                    f"service '{self.name}': config_files, validate, unit and package "
                    "are only allowed when kind = 'os'"
                )
        return self


def unit_base(unit: str) -> str:
    return unit.removesuffix(".service")


class AppConfig(_Strict):
    vps: TargetVPS
    policy: Policy = Field(default_factory=Policy)
    service: list[ServiceDeclaration] = Field(default_factory=list)
    migration: MigrationSettings | None = None

    @model_validator(mode="after")
    def _cross_checks(self) -> AppConfig:
        seen: set[tuple[str, str]] = set()
        protected_units = {unit_base(u) for u in self.policy.remote_access_units}
        for svc in self.service:
            if svc.key in seen:
                raise ValueError(f"duplicate service declaration ({svc.name}, {svc.kind.value})")
            seen.add(svc.key)
            if svc.kind is ServiceKind.OS and unit_base(svc.unit_name) in protected_units:
                raise ValueError(
                    f"service '{svc.name}' uses remote-access unit '{svc.unit_name}', "
                    "which can never be managed"
                )
        return self

    @property
    def secret_strings(self) -> list[str]:
        """Strings that must never appear in output, logs or audit records (FR-033)."""
        secrets = [self.vps.key_path]
        if self.vps.key_passphrase_env:
            secrets.append(os.environ.get(self.vps.key_passphrase_env))
        return [s for s in secrets if s]
