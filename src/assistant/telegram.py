"""Telegram Bot API channel: outbound long polling only (research R3)."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import httpx2

from assistant.format import plain, to_html
from assistant.state import State
from vps_mcp.remote.redact import install_redaction, register_secret, scrub

log = logging.getLogger(__name__)
MAX_TEXT = 4000
API = "https://api.telegram.org"


@dataclass
class Message:
    chat_id: int
    user_id: int
    text: str
    date: datetime


@dataclass
class Callback:
    id: str
    chat_id: int
    user_id: int
    data: str
    message_id: int


Update = Message | Callback
Button = tuple[str, str]  # (label, callback_data)


def split_text(text: str, limit: int = MAX_TEXT) -> list[str]:
    text = text.strip() or "(empty)"
    if len(text) <= limit:
        return [text]
    parts: list[str] = []
    cur = ""
    for para in text.split("\n\n"):
        while len(para) > limit:
            cut = para.rfind("\n", 0, limit)
            cut = cut if cut > limit // 2 else limit
            if cur:
                parts.append(cur)
                cur = ""
            parts.append(para[:cut])
            para = para[cut:].lstrip("\n")
        candidate = f"{cur}\n\n{para}" if cur else para
        if len(candidate) > limit:
            parts.append(cur)
            cur = para
        else:
            cur = candidate
    if cur:
        parts.append(cur)
    n = len(parts)
    return [f"({i}/{n})\n{p}" for i, p in enumerate(parts, 1)] if n > 1 else parts


class TelegramChannel:
    def __init__(
        self,
        token: str,
        allowed_ids: list[int],
        state: State,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        poll_timeout: int = 50,
    ) -> None:
        self._token = token
        register_secret(token)
        for name in ("httpx2", "httpcore"):  # their request logs include the URL (and token)
            lg = logging.getLogger(name)
            if not any(getattr(f, "secrets", None) == [token] for f in lg.filters):
                install_redaction(lg, [token])
        self.allowed = set(allowed_ids)
        self.state = state
        self.poll_timeout = poll_timeout
        kw: dict[str, Any] = {"timeout": httpx2.Timeout(poll_timeout + 15, connect=10.0)}
        if transport is not None:
            kw["transport"] = transport
        self._http = httpx2.AsyncClient(**kw)
        self.backoff = 1.0
        self.errors = 0

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, **params: Any) -> Any:
        url = f"{API}/bot{self._token}/{method}"
        resp = await self._http.post(url, json={k: v for k, v in params.items() if v is not None})
        data = resp.json()
        if not data.get("ok"):
            raise RuntimeError(
                f"telegram {method} failed: {data.get('description', resp.status_code)}"
            )
        return data.get("result")

    # ----------------------------------------------------------- receiving

    async def poll_once(self) -> list[Update]:
        offset = int(self.state.kv_get("tg_offset", "0") or 0)
        result = await self._call(
            "getUpdates",
            offset=offset or None,
            timeout=self.poll_timeout,
            allowed_updates=["message", "callback_query"],
        )
        out: list[Update] = []
        for upd in result or []:
            self.state.kv_set("tg_offset", str(int(upd["update_id"]) + 1))
            parsed = self._parse(upd)
            if parsed is not None:
                out.append(parsed)
        return out

    def _parse(self, upd: dict[str, Any]) -> Update | None:
        if "message" in upd:
            m = upd["message"]
            chat, user = m.get("chat") or {}, m.get("from") or {}
            uid = int(user.get("id", 0))
            if chat.get("type") != "private" or uid not in self.allowed:
                self.state.audit(
                    "access_denied",
                    origin="telegram",
                    target=f"user {uid} chat {chat.get('type')}",
                    outcome="ignored",
                )
                return None
            text = m.get("text")
            if not isinstance(text, str):
                return None
            return Message(
                int(chat["id"]), uid, text, datetime.fromtimestamp(int(m.get("date", 0)), UTC)
            )
        if "callback_query" in upd:
            q = upd["callback_query"]
            user = q.get("from") or {}
            uid = int(user.get("id", 0))
            msg = q.get("message") or {}
            chat = msg.get("chat") or {}
            if uid not in self.allowed or chat.get("type") != "private":
                self.state.audit(
                    "access_denied",
                    origin="telegram",
                    target=f"callback from {uid}",
                    outcome="ignored",
                )
                return None
            return Callback(
                str(q["id"]),
                int(chat["id"]),
                uid,
                str(q.get("data", "")),
                int(msg.get("message_id", 0)),
            )
        return None

    async def updates(self) -> AsyncIterator[Update]:
        while True:
            try:
                batch = await self.poll_once()
                self.backoff = 1.0
                self.errors = 0
            except (httpx2.HTTPError, RuntimeError, ValueError) as exc:
                self.errors += 1
                log.warning(
                    "telegram poll failed (%s); retrying in %.0fs",
                    exc.__class__.__name__,
                    self.backoff,
                )
                await asyncio.sleep(self.backoff * (0.8 + 0.4 * random.random()))  # noqa: S311
                self.backoff = min(60.0, self.backoff * 2)
                continue
            for u in batch:
                yield u

    # ------------------------------------------------------------- sending

    @staticmethod
    def _markup(buttons: list[Button] | None) -> dict[str, Any] | None:
        if not buttons:
            return None
        return {
            "inline_keyboard": [[{"text": label, "callback_data": data} for label, data in buttons]]
        }

    async def send(
        self,
        chat_id: int,
        text: str,
        buttons: list[Button] | None = None,
        *,
        markdown: bool = False,
    ) -> int:
        """Send text (split if long). markdown=True renders the model's Markdown as HTML."""
        parts = split_text(scrub(text, []))  # never send a known secret, whatever produced it
        mid = 0
        for i, part in enumerate(parts):
            markup = self._markup(buttons) if i == len(parts) - 1 else None
            res = None
            if markdown:
                body = to_html(part)
                if len(body) <= 4096:
                    try:
                        res = await self._call(
                            "sendMessage",
                            chat_id=chat_id,
                            text=body,
                            parse_mode="HTML",
                            reply_markup=markup,
                            disable_web_page_preview=True,
                        )
                    except RuntimeError as exc:  # Telegram rejected the markup: send it plain
                        log.info("html rejected, sending plain: %s", exc)
                part = plain(part)
            if res is None:
                res = await self._call(
                    "sendMessage",
                    chat_id=chat_id,
                    text=part,
                    reply_markup=markup,
                    disable_web_page_preview=True,
                )
            mid = int(res.get("message_id", 0)) if isinstance(res, dict) else 0
        return mid

    async def edit(
        self, chat_id: int, message_id: int, text: str, buttons: list[Button] | None = None
    ) -> None:
        try:
            await self._call(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=split_text(scrub(text, []))[0],
                reply_markup=self._markup(buttons),
            )
        except (httpx2.HTTPError, RuntimeError) as exc:
            log.info("edit failed: %s", exc.__class__.__name__)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        try:
            await self._call(
                "answerCallbackQuery", callback_query_id=callback_id, text=text or None
            )
        except (httpx2.HTTPError, RuntimeError):
            pass

    async def typing(self, chat_id: int) -> None:
        try:
            await self._call("sendChatAction", chat_id=chat_id, action="typing")
        except (httpx2.HTTPError, RuntimeError):
            pass
