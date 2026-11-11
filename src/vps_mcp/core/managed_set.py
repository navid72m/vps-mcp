"""The effective allowlist: declared ∪ deployed − removed (FR-025, data-model.md §3)."""

from __future__ import annotations

from typing import Any

from vps_mcp.config.policy import AppConfig, ServiceDeclaration, unit_base
from vps_mcp.errors import invalid, not_permitted
from vps_mcp.models.inputs import ServiceKind, ServiceName, validated
from vps_mcp.store.services import ServicesStore


class ManagedSet:
    def __init__(self, config: AppConfig, services: ServicesStore) -> None:
        self.config = config
        self.services = services
        self._remote_access = {unit_base(u) for u in config.policy.remote_access_units}

    def declared(self, name: str, kind: str) -> ServiceDeclaration | None:
        for d in self.config.service:
            if d.name == name and d.kind.value == kind:
                return d
        return None

    def all(self) -> list[ServiceDeclaration]:
        rows = {(r.name, r.kind): r for r in self.services.list_rows()}
        result: dict[tuple[str, str], ServiceDeclaration] = {}
        for d in self.config.service:
            row = rows.get(d.key)
            if row is None or not row.removed:
                result[d.key] = d
        for key, row in rows.items():
            if row.removed or key in result:
                continue
            if row.origin == "deployed" and row.spec and "declaration" in row.spec:
                result[key] = ServiceDeclaration.model_validate(row.spec["declaration"])
        return [d for d in result.values() if not self.is_remote_access(d)]

    def deployed_spec(self, name: str, kind: str) -> dict[str, Any] | None:
        for r in self.services.list_rows():
            if r.name == name and r.kind == kind and not r.removed and r.origin == "deployed":
                return r.spec
        return None

    def is_remote_access(self, decl: ServiceDeclaration) -> bool:
        if decl.kind is ServiceKind.OS:
            return (
                unit_base(decl.unit_name) in self._remote_access or decl.name in self._remote_access
            )
        return False

    def is_remote_access_name(self, name: str) -> bool:
        return unit_base(name) in self._remote_access

    def resolve(self, name: str, kind: str | None) -> ServiceDeclaration:
        validated(ServiceName, name, "service")
        if kind is not None:
            kind = validated(ServiceKind, kind, "kind").value
        if self.is_remote_access_name(name):
            raise not_permitted(name)
        matches = [
            d for d in self.all() if d.name == name and (kind is None or d.kind.value == kind)
        ]
        if not matches:
            raise not_permitted(name)
        if len(matches) > 1:
            raise invalid(
                f"service '{name}' exists as both os and container: ambiguous: specify kind"
            )
        return matches[0]
