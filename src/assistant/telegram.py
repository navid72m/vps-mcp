"""Telegram Bot API channel: outbound long polling only (research R3)."""

from __future__ import annotations

import asyncio
import logging
import random
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx2

from assistant.format import plain, to_html
from assistant.media import MAX_DOWNLOAD, Attachment, pick_photo
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
    attachments: list[Attachment] = field(default_factory=list)  # photo / document (010)


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
            atts: list[Attachment] = []
            if isinstance(m.get("photo"), list) and m["photo"]:
                best = pick_photo(m["photo"])
                if best:
                    atts.append(
                        Attachment(
                            "photo",
                            str(best["file_id"]),
                            "photo.jpg",
                            "image/jpeg",
                            int(best.get("file_size", 0)),
                        )
                    )
            elif isinstance(m.get("document"), dict):
                d = m["document"]
                atts.append(
                    Attachment(
                        "document",
                        str(d["file_id"]),
                        str(d.get("file_name") or "file"),
                        str(d.get("mime_type") or ""),
                        int(d.get("file_size", 0)),
                    )
                )
            if atts:
                text = m.get("caption") or ""
            if not isinstance(text, str):
                return None
            return Message(
                int(chat["id"]),
                uid,
                text,
                datetime.fromtimestamp(int(m.get("date", 0)), UTC),
                atts,
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

    async def send_document(
        self,
        chat_id: int,
        path: Path | str,
        caption: str = "",
    ) -> int:
        """Send a document or file to Telegram chat via multipart upload (up to 50MB)."""

        def _read() -> tuple[str, bytes]:
            resolved = Path(path).expanduser().resolve()
            if not resolved.is_file():
                raise FileNotFoundError(f"file not found: {resolved}")
            return resolved.name, resolved.read_bytes()

        fname, content = await asyncio.to_thread(_read)
        url = f"{API}/bot{self._token}/sendDocument"
        data: dict[str, str] = {"chat_id": str(chat_id)}
        if caption:
            data["caption"] = caption[:1024]
        resp = await self._http.post(
            url,
            data=data,
            files={"document": (fname, content, "application/octet-stream")},
            timeout=120.0,
        )
        res_data = resp.json()
        if not res_data.get("ok"):
            raise RuntimeError(
                f"telegram sendDocument failed: {res_data.get('description', resp.status_code)}"
            )
        result = res_data.get("result", {})
        return int(result.get("message_id", 0)) if isinstance(result, dict) else 0

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

    async def delete(self, chat_id: int, message_id: int) -> None:
        """Delete a message from chat (e.g. temporary waiting indicator)."""
        if not message_id:
            return
        try:
            await self._call("deleteMessage", chat_id=chat_id, message_id=message_id)
        except (httpx2.HTTPError, RuntimeError) as exc:
            log.info("delete failed: %s", exc.__class__.__name__)

    async def download(self, file_id: str, limit: int = MAX_DOWNLOAD) -> bytes:
        """A file the operator sent (Bot API getFile; bots may download up to 20 MB)."""
        info = await self._call("getFile", file_id=file_id)
        path = str((info or {}).get("file_path") or "")
        if not path:
            raise RuntimeError("telegram returned no file path")
        if int((info or {}).get("file_size") or 0) > limit:
            raise ValueError("file too large")
        resp = await self._http.get(f"{API}/file/bot{self._token}/{path}")
        if resp.status_code >= 400:
            raise RuntimeError(f"telegram file download failed: HTTP {resp.status_code}")
        if len(resp.content) > limit:
            raise ValueError("file too large")
        return bytes(resp.content)

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
