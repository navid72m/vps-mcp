"""MCP servers as tools: allowlists, policy, previews, approvals (research R4-R6)."""

from __future__ import annotations

import contextlib
import fnmatch
import json
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from mcp import Client, StdioServerParameters

from assistant.approvals import Approvals
from assistant.budget import Budget, BudgetExceeded
from assistant.config import ServerSection
from assistant.providers import ToolSpec
from assistant.state import State

log = logging.getLogger(__name__)
RESULT_CAP = 12 * 1024
NAME_RE = re.compile(r"[^a-zA-Z0-9_-]")


@dataclass
class HubTool:
    name: str  # model-facing <server>__<tool>
    server: str
    tool: str
    description: str
    schema: dict[str, Any]
    read_only: bool
    policy: str  # auto | ask | never
    preview: bool  # confirm is a required parameter -> confirm=false is a safe preview


@dataclass
class ToolOutcome:
    content: str
    is_error: bool = False
    status: str = "ok"  # ok | error | refused | declined | budget


@dataclass
class Server:
    section: ServerSection
    target: Any  # StdioServerParameters | in-process server
    client: Client | None = None
    stack: contextlib.AsyncExitStack | None = None
    up: bool = False
    error: str | None = None
    tools: list[HubTool] = field(default_factory=list)


def envelope(server: str, tool: str, content: str, ok: bool = True, **extra: Any) -> str:
    body = {
        "kind": "tool_output",
        "server": server,
        "tool": tool,
        "untrusted": True,
        "ok": ok,
        **extra,
        "content": content,
    }
    return json.dumps(body, ensure_ascii=False)


def _result_text(res: Any) -> str:
    if getattr(res, "structured_content", None) is not None:
        text = json.dumps(
            res.structured_content, ensure_ascii=False, default=str, separators=(",", ":")
        )
    else:
        text = "\n".join(getattr(c, "text", "") for c in getattr(res, "content", []) or [])
    if len(text) > RESULT_CAP:
        text = text[:RESULT_CAP] + "…(truncated)"
    return text


class ToolHub:
    def __init__(
        self,
        servers: list[ServerSection],
        state: State,
        *,
        approvals: Approvals | None = None,
        budget: Budget | None = None,
        mask: Callable[[str], str] = lambda s: s,
        in_process: dict[str, Any] | None = None,
    ) -> None:
        self.state = state
        self.approvals = approvals
        self.budget = budget
        self.mask = mask
        self.servers: dict[str, Server] = {}
        for s in servers:
            target = (in_process or {}).get(s.name) or StdioServerParameters(
                command=s.command[0], args=s.command[1:]
            )
            self.servers[s.name] = Server(s, target)

    # ---------------------------------------------------------- lifecycle

    async def connect(self) -> None:
        for srv in self.servers.values():
            await self._connect(srv)

    async def _connect(self, srv: Server) -> None:
        await self._disconnect(srv)
        stack = contextlib.AsyncExitStack()
        try:
            client = await stack.enter_async_context(Client(srv.target))
            listed = await client.list_tools()
        except Exception as exc:  # noqa: BLE001 - a down server must not stop the assistant
            await stack.aclose()
            srv.up, srv.error = False, f"{exc.__class__.__name__}"
            log.warning("tool server %s unavailable: %s", srv.section.name, srv.error)
            return
        srv.client, srv.stack, srv.up, srv.error = client, stack, True, None
        srv.tools = [t for t in (self._tool(srv.section, x) for x in listed.tools) if t]

    async def _disconnect(self, srv: Server) -> None:
        if srv.stack is not None:
            with contextlib.suppress(Exception):
                await srv.stack.aclose()
        srv.client, srv.stack, srv.up = None, None, False

    async def close(self) -> None:
        for srv in self.servers.values():
            await self._disconnect(srv)

    def _tool(self, sec: ServerSection, t: Any) -> HubTool | None:
        name = str(t.name)
        if not any(fnmatch.fnmatchcase(name, g) for g in sec.allow):
            return None
        ann = getattr(t, "annotations", None)
        read_only = bool(getattr(ann, "read_only_hint", False)) if ann else False
        explicit = next((v for k, v in sec.policy.items() if fnmatch.fnmatchcase(name, k)), None)
        if explicit == "never" or any(
            v == "never" and fnmatch.fnmatchcase(name, k) for k, v in sec.policy.items()
        ):
            policy = "never"
        elif explicit:
            policy = explicit
        else:
            policy = "auto" if read_only else "ask"
        schema = dict(getattr(t, "input_schema", None) or {"type": "object", "properties": {}})
        required = schema.get("required") or []
        props = schema.get("properties") or {}
        model_name = NAME_RE.sub("_", f"{sec.name}__{name}")[:64]
        return HubTool(
            model_name,
            sec.name,
            name,
            str(t.description or "")[:1024],
            schema,
            read_only,
            policy,
            preview="confirm" in props and "confirm" in required,
        )

    def status(self) -> list[dict[str, Any]]:
        out = []
        for srv in self.servers.values():
            counts = {
                p: sum(1 for t in srv.tools if t.policy == p) for p in ("auto", "ask", "never")
            }
            out.append(
                {
                    "server": srv.section.name,
                    "up": srv.up,
                    "error": srv.error,
                    "tools": len(srv.tools),
                    **counts,
                }
            )
        return out

    def specs(self) -> list[ToolSpec]:
        specs = []
        for srv in self.servers.values():
            for t in srv.tools:
                if t.policy == "never" or not srv.up:
                    continue
                schema = json.loads(json.dumps(t.schema))
                if t.preview or "confirm" in (schema.get("properties") or {}):
                    # the hub owns confirm/plan ids: the model never sets them
                    for k in ("confirm", "plan_id", "plan_hash"):
                        (schema.get("properties") or {}).pop(k, None)
                    schema["required"] = [
                        r
                        for r in schema.get("required") or []
                        if r not in ("confirm", "plan_id", "plan_hash")
                    ]
                hint = " [needs the operator's approval]" if t.policy == "ask" else ""
                specs.append(ToolSpec(t.name, t.description + hint, schema))
        return specs

    def find(self, name: str) -> HubTool | None:
        for srv in self.servers.values():
            for t in srv.tools:
                if t.name == name:
                    return t
        return None

    # --------------------------------------------------------------- call

    async def _raw(self, t: HubTool, args: dict[str, Any]) -> Any:
        srv = self.servers[t.server]
        for attempt in (1, 2):
            if not srv.up or srv.client is None:
                await self._connect(srv)
            if srv.client is None:
                raise RuntimeError(f"tool server '{t.server}' is unavailable")
            try:
                return await srv.client.call_tool(t.tool, args)
            except Exception:  # noqa: BLE001 - reconnect once on a broken session
                srv.up = False
                if attempt == 2:
                    raise
        raise RuntimeError("unreachable")

    async def call(
        self, name: str, args: dict[str, Any], *, origin: str, conversation: str, chat_id: int
    ) -> ToolOutcome:
        t = self.find(name)
        if t is None:
            self.state.audit(
                "refusal",
                conversation=conversation,
                origin=origin,
                tool=name[:64],
                outcome="not allowlisted",
            )
            return ToolOutcome(
                envelope("-", name, "this tool is not available", ok=False, refused=True),
                True,
                "refused",
            )
        if t.policy == "never":
            self.state.audit(
                "refusal",
                conversation=conversation,
                origin=origin,
                server=t.server,
                tool=t.tool,
                outcome="policy never",
            )
            return ToolOutcome(
                envelope(
                    t.server,
                    t.tool,
                    "this tool is not allowed for the assistant",
                    ok=False,
                    refused=True,
                ),
                True,
                "refused",
            )
        if self.budget is not None:
            try:
                self.budget.check_tool()
            except BudgetExceeded as exc:
                return ToolOutcome(envelope(t.server, t.tool, str(exc), ok=False), True, "budget")
        clean = {k: v for k, v in args.items() if k not in ("confirm", "plan_id", "plan_hash")}
        needs_ask = t.policy == "ask" or (origin == "schedule" and not t.read_only)
        target = self.mask(json.dumps(clean, default=str))[:200]
        if not needs_ask:
            return await self._execute(t, clean, origin, conversation, target)
        if self.approvals is None:
            return ToolOutcome(
                envelope(t.server, t.tool, "approvals unavailable", ok=False), True, "refused"
            )
        preview_text: str | None = None
        extra: dict[str, Any] = {}
        if t.preview:
            try:
                res = await self._raw(t, {**clean, "confirm": False})
            except Exception as exc:  # noqa: BLE001
                return ToolOutcome(
                    envelope(
                        t.server, t.tool, f"preview failed: {exc.__class__.__name__}", ok=False
                    ),
                    True,
                    "error",
                )
            preview_text = self.mask(_result_text(res))
            if getattr(res, "is_error", False):
                self.state.audit(
                    "tool_call",
                    conversation=conversation,
                    origin=origin,
                    server=t.server,
                    tool=t.tool,
                    target=target,
                    outcome="preview refused",
                )
                return ToolOutcome(
                    envelope(t.server, t.tool, preview_text, ok=False), True, "error"
                )
            sc = getattr(res, "structured_content", None) or {}
            for key in ("plan_id", "plan_hash"):
                val = (
                    sc.get(key) or (sc.get("plan") or {}).get(key) if isinstance(sc, dict) else None
                )
                if val and key in (t.schema.get("properties") or {}):
                    extra[key] = val
        final_args = {
            **clean,
            **({"confirm": True} if "confirm" in (t.schema.get("properties") or {}) else {}),
            **extra,
        }
        shown = {k: self.mask(json.dumps(v, default=str)) for k, v in clean.items()}
        outcome = await self.approvals.request(
            chat_id=chat_id,
            conversation=conversation,
            server=t.server,
            tool=t.tool,
            args=final_args,
            shown_args=shown,
            preview=preview_text,
            origin=origin,
        )
        if outcome.status != "approved":
            reason = {
                "rejected": "rejected by the operator",
                "expired": "approval expired",
                "voided": "cancelled (assistant stopped or conversation reset)",
            }[outcome.status]
            return ToolOutcome(
                json.dumps(
                    {"kind": "tool_output", "server": t.server, "tool": t.tool, "declined": reason}
                ),
                True,
                "declined",
            )
        if not self.approvals.bound(outcome.approval_id, t.server, t.tool, final_args):
            return ToolOutcome(
                envelope(t.server, t.tool, "approval does not match the call", ok=False),
                True,
                "refused",
            )
        self.approvals.mark_executed(outcome.approval_id)
        return await self._execute(t, final_args, origin, conversation, target)

    async def _execute(
        self, t: HubTool, args: dict[str, Any], origin: str, conversation: str, target: str
    ) -> ToolOutcome:
        try:
            res = await self._raw(t, args)
        except Exception as exc:  # noqa: BLE001
            self.state.audit(
                "tool_call",
                conversation=conversation,
                origin=origin,
                server=t.server,
                tool=t.tool,
                target=target,
                outcome="error",
            )
            return ToolOutcome(
                envelope(t.server, t.tool, f"tool failed: {exc.__class__.__name__}", ok=False),
                True,
                "error",
            )
        if self.budget is not None:
            self.budget.record_tool()
        is_err = bool(getattr(res, "is_error", False))
        self.state.audit(
            "tool_call",
            conversation=conversation,
            origin=origin,
            server=t.server,
            tool=t.tool,
            target=target,
            outcome="error" if is_err else "ok",
        )
        return ToolOutcome(
            envelope(t.server, t.tool, self.mask(_result_text(res)), ok=not is_err),
            is_err,
            "error" if is_err else "ok",
        )
