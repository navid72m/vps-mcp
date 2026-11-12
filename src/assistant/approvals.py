"""Phone approvals: exact-call binding, HMAC buttons, expiry, voiding (research R7)."""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import secrets as _secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any, Protocol

from assistant.clock import Clock
from assistant.state import State, new_id

PREVIEW_CAP = 2500


class Sender(Protocol):
    async def send(
        self, chat_id: int, text: str, buttons: list[tuple[str, str]] | None = None
    ) -> int: ...
    async def edit(
        self, chat_id: int, message_id: int, text: str, buttons: list[tuple[str, str]] | None = None
    ) -> None: ...
    async def answer_callback(self, callback_id: str, text: str = "") -> None: ...


def args_hash(server: str, tool: str, args: dict[str, Any]) -> str:
    canon = json.dumps([server, tool, args], sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


@dataclass
class Outcome:
    status: str  # approved | rejected | expired | voided
    approval_id: str


class Signer:
    def __init__(self) -> None:
        self.key = _secrets.token_bytes(32)

    def sign(self, payload: str) -> str:
        return f"{payload}:{hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()[:8]}"

    def verify(self, data: str) -> str | None:
        payload, _, mac = data.rpartition(":")
        good = hmac.new(self.key, payload.encode(), hashlib.sha256).hexdigest()[:8]
        return payload if payload and hmac.compare_digest(mac, good) else None


class Approvals:
    def __init__(
        self,
        state: State,
        channel: Sender,
        clock: Clock,
        minutes: int,
        signer: Signer,
        poll: float = 0.05,
    ) -> None:
        self.state = state
        self.channel = channel
        self.clock = clock
        self.minutes = minutes
        self.signer = signer
        self.poll = poll
        self._futures: dict[str, asyncio.Future[str]] = {}

    async def request(
        self,
        *,
        chat_id: int,
        conversation: str,
        server: str,
        tool: str,
        args: dict[str, Any],
        shown_args: dict[str, Any],
        preview: str | None,
        origin: str,
    ) -> Outcome:
        aid = new_id("a")
        now = self.clock.wall()
        expires = now + timedelta(minutes=self.minutes)
        prev = (preview or "no preview available for this tool").strip()
        if len(prev) > PREVIEW_CAP:
            prev = prev[:PREVIEW_CAP] + "\n… (preview truncated)"
        arg_lines = (
            "\n".join(f"{k}: {json.dumps(v, default=str)[:200]}" for k, v in shown_args.items())
            or "(no arguments)"
        )
        text = (
            f"🔐 Approve this action?{' (scheduled run)' if origin == 'schedule' else ''}\n"
            f"{server} · {tool}\n{arg_lines}\n\nPreview:\n{prev}\n\n"
            f"Expires in {self.minutes} min."
        )
        self.state.approval_add(
            id=aid,
            conversation=conversation,
            server=server,
            tool=tool,
            args_hash=args_hash(server, tool, args),
            args_json=json.dumps(shown_args, default=str)[:4000],
            preview=prev,
            created=now.isoformat(),
            expires=expires.isoformat(),
            message_id=None,
            status="pending",
        )
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._futures[aid] = fut
        mid = await self.channel.send(
            chat_id,
            text,
            [
                ("✅ Approve", self.signer.sign(f"ap:{aid}:a")),
                ("❌ Reject", self.signer.sign(f"ap:{aid}:r")),
            ],
        )
        self.state.approval_set(aid, message_id=mid)
        self.state.audit(
            "approval_request",
            conversation=conversation,
            origin=origin,
            server=server,
            tool=tool,
            target=arg_lines.splitlines()[0][:200],
            outcome="pending",
        )
        try:
            while not fut.done():
                if self.clock.wall() >= expires:
                    fut.set_result("expired")
                    break
                await asyncio.wait({fut}, timeout=self.poll)
            status = fut.result()
        finally:
            self._futures.pop(aid, None)
        row = self.state.approval_get(aid)
        if row and row["status"] == "pending":
            self.state.approval_set(aid, status=status)
        label = {
            "approved": "✅ Approved",
            "rejected": "❌ Rejected",
            "expired": "⌛ Expired",
            "voided": "⛔ Voided",
        }[status]
        await self.channel.edit(chat_id, mid, f"{label} · {server} · {tool}\n{arg_lines}")
        self.state.audit(
            "approval_outcome",
            conversation=conversation,
            origin=origin,
            server=server,
            tool=tool,
            outcome=status,
        )
        return Outcome(status, aid)

    def bound(self, approval_id: str, server: str, tool: str, args: dict[str, Any]) -> bool:
        row = self.state.approval_get(approval_id)
        return bool(
            row
            and row["status"] == "approved"
            and row["args_hash"] == args_hash(server, tool, args)
        )

    def mark_executed(self, approval_id: str) -> None:
        self.state.approval_set(approval_id, status="executed")

    async def on_callback(self, data: str, callback_id: str) -> bool:
        payload = self.signer.verify(data)
        parts = payload.split(":") if payload else []
        if len(parts) != 3 or parts[0] != "ap" or parts[2] not in ("a", "r"):
            await self.channel.answer_callback(callback_id, "expired or already used")
            return False
        aid, choice = parts[1], parts[2]
        row = self.state.approval_get(aid)
        fut = self._futures.get(aid)
        if (
            row is None
            or row["status"] != "pending"
            or fut is None
            or fut.done()
            or self.clock.wall().isoformat() >= row["expires"]
        ):
            await self.channel.answer_callback(callback_id, "expired or already used")
            return False
        status = "approved" if choice == "a" else "rejected"
        self.state.approval_set(aid, status=status)
        fut.set_result(status)
        await self.channel.answer_callback(callback_id, "approved" if choice == "a" else "rejected")
        return True

    def void_all(self) -> int:
        n = 0
        for row in self.state.approvals_pending():
            self.state.approval_set(row["id"], status="voided")
            fut = self._futures.get(row["id"])
            if fut is not None and not fut.done():
                fut.set_result("voided")
            n += 1
        return n
