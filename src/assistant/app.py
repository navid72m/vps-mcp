"""Wiring: lock, Telegram dispatcher, turns, approvals, proposals, scheduler, heartbeat (R7-R13)."""

from __future__ import annotations

import asyncio
import contextlib
import fcntl
import json
import logging
import os
import signal
import subprocess
import sys
import zoneinfo
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from assistant import (
    calendar,
    commands,
    files,
    gmail,
    google_auth,
    knowledge_base,
    knowme,
    knowme_runtime,
    media,
    telegram_user,
    web,
)
from assistant.agent import Agent
from assistant.approvals import Approvals, Signer
from assistant.budget import Budget, BudgetExceeded
from assistant.clock import Clock, RealClock
from assistant.config import Config
from assistant.media import Attachment
from assistant.memory import AssistantMemory
from assistant.providers import ImageBlock, Provider, ToolSpec
from assistant.providers.router import ModelRouter
from assistant.scheduler import Cadence, SleepDetector
from assistant.secrets import SecretMissing
from assistant.state import State, new_id
from assistant.telegram import Callback, Message, TelegramChannel
from assistant.toolhub import ToolHub

log = logging.getLogger(__name__)


FILE_CHANGES = ("write", "edit", "delete", "send")


def file_preview(op: str, target: Path, args: dict[str, Any]) -> str:
    try:
        size = target.stat().st_size if target.is_file() else None
    except OSError:
        size = None
    where = f"{target}" + (f" ({size} bytes)" if size is not None else "")
    if op == "write":
        content = str(args.get("content", ""))
        head = "overwrite" if target.exists() else "create"
        if target.exists() and not args.get("overwrite"):
            head = "write (the file exists; overwrite=false will make it fail)"
        return f"{head} {where}\n--- new content ({len(content)} chars) ---\n{content[:600]}"
    if op == "edit":
        old, new = str(args.get("target", "")), str(args.get("replacement", ""))
        return f"edit {where}\n--- replace ---\n{old[:300]}\n--- with ---\n{new[:300]}"
    if op == "delete":
        kind = "directory (must be empty)" if target.is_dir() else "file"
        return f"DELETE {kind} {where}"
    return f"send {where} to this Telegram chat"


class AlreadyRunning(Exception):
    pass


class InstanceLock:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.fd: int | None = None

    def acquire(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            other = os.read(fd, 32).decode(errors="ignore").strip()
            os.close(fd)
            raise AlreadyRunning(other or "?") from None
        os.ftruncate(fd, 0)
        os.write(fd, str(os.getpid()).encode())
        self.fd = fd

    def release(self) -> None:
        if self.fd is not None:
            fcntl.flock(self.fd, fcntl.LOCK_UN)
            os.close(self.fd)
            self.fd = None


class _WaitIndicator:
    """Refreshes typing status and displays an elapsed-time spinner if a turn takes longer than delay_s."""

    def __init__(
        self,
        channel: TelegramChannel,
        chat_id: int,
        delay_s: float = 3.5,
        interval_s: float = 2.5,
    ) -> None:
        self.channel = channel
        self.chat_id = chat_id
        self.delay_s = delay_s
        self.interval_s = interval_s
        self.msg_id: int = 0
        self.status: str = "Thinking..."
        self._stopped = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._spinners = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]

    def set_status(self, status: str) -> None:
        self.status = status

    async def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _run(self) -> None:
        loop = asyncio.get_running_loop()
        start_time = loop.time()
        last_typing = 0.0
        spin_idx = 0

        try:
            while not self._stopped.is_set():
                now = loop.time()
                elapsed = int(now - start_time)

                if now - last_typing >= 4.0:
                    with contextlib.suppress(Exception):
                        await self.channel.typing(self.chat_id)
                    last_typing = now

                if (now - start_time) >= self.delay_s:
                    spin = self._spinners[spin_idx % len(self._spinners)]
                    spin_idx += 1
                    text = f"{spin} {self.status} ({elapsed}s)"
                    try:
                        if not self.msg_id:
                            self.msg_id = await self.channel.send(self.chat_id, text)
                        else:
                            await self.channel.edit(self.chat_id, self.msg_id, text)
                    except Exception as exc:
                        log.debug("failed to update wait indicator: %s", exc)

                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._stopped.wait(), timeout=self.interval_s)
        except asyncio.CancelledError:
            pass

    async def stop(self) -> None:
        self._stopped.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self.msg_id:
            with contextlib.suppress(Exception):
                await self.channel.delete(self.chat_id, self.msg_id)
            self.msg_id = 0


class Assistant:
    def __init__(
        self,
        config: Config,
        *,
        provider: Provider,
        channel: TelegramChannel,
        state: State,
        hub: ToolHub,
        memory: AssistantMemory,
        clock: Clock | None = None,
        approval_poll: float = 0.05,
        coalesce_s: float = 1.5,
    ) -> None:
        self.config = config
        self.clock = clock or RealClock()
        self.state = state
        self.channel = channel
        self.memory = memory
        self.tz = zoneinfo.ZoneInfo(config.assistant.timezone)
        self.budget = Budget(config, state, self.clock)
        self.signer = Signer()
        self.approvals = Approvals(
            state,
            channel,
            self.clock,
            config.limits.approval_minutes,
            self.signer,
            poll=approval_poll,
        )
        hub.approvals, hub.budget = self.approvals, self.budget
        self.hub = hub
        self.agent = Agent(config, provider, hub, memory, self.budget, state, self.clock)
        self.router = provider if isinstance(provider, ModelRouter) else None
        specs = self.schedule_specs()
        specs.extend(self.files_specs())
        specs.extend(knowme.SPECS)
        if config.web.enabled:
            specs.extend(web.specs())
        if config.google.enabled:
            specs.extend(self.calendar_specs())
            specs.extend(self.email_specs())
        if config.telegram_user.enabled:
            specs.extend(self.tg_user_specs())
        if config.knowledge_base.enabled:
            specs.extend(self.kb_specs())
        self.web_transport: Any = None  # tests inject a mock transport
        self.google_transport: Any = None  # tests inject a mock transport
        self.tg_user_client: telegram_user.TelegramUserClient | None = None
        self.kb: knowledge_base.KnowledgeBase | None = None
        self.web_resolver: web.Resolver = web.default_resolver
        if self.router is not None:
            self.budget.prices = self.router.profile.prices
            if len(self.router.profiles) > 1:
                specs.append(self.switch_spec())
        self.agent.builtin_specs = specs
        self.agent.builtin = self.builtin
        self.owner_chat = config.telegram.allowed_user_ids[0]
        self.turns: dict[int, asyncio.Task[Any]] = {}
        self.pending: dict[int, list[str]] = {}  # messages waiting for the next turn, per chat
        self.pending_files: dict[int, list[Attachment]] = {}  # their photos and documents
        # facts mentioned without a request to remember: offered at the end of the conversation
        self.pending_suggestions: dict[str, list[dict[str, Any]]] = {}
        self.coalesce_s = coalesce_s
        self.wait_indicator_delay_s: float = 3.5
        self.wait_indicator_interval_s: float = 2.5
        self.background: set[asyncio.Task[Any]] = set()
        self.started = self.clock.wall()
        self.sleep = SleepDetector(self.clock)
        self.scheduler_task: asyncio.Task[Any] | None = None

    # ----------------------------------------------------------- state flags

    @property
    def paused(self) -> bool:
        return self.state.kv_get("paused") == "1"

    def spawn(self, coro: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(coro)
        self.background.add(task)
        task.add_done_callback(self.background.discard)
        return task

    # -------------------------------------------------------------- startup

    async def start(self) -> None:
        await self.hub.connect()
        self.seed_schedules()
        if self.state.kv_get("clean_shutdown") == "0":
            self.state.audit("system", origin="system", outcome="restarted after a crash")
            with contextlib.suppress(Exception):
                await self.channel.send(
                    self.owner_chat,
                    "♻️ I restarted after an unexpected stop. "
                    "Pending approvals from before are no longer valid.",
                )
        self.state.kv_set("clean_shutdown", "0")
        for row in self.state.approvals_pending():
            self.state.approval_set(row["id"], status="voided")
        if self.router is not None and self.router.profile.local:
            self.spawn(self.warm_up(self.router.current))

    async def warm_up(self, alias: str) -> None:
        assert self.router is not None
        t0 = self.clock.mono()
        try:
            await self.agent.warm_up(self.router.provider(alias))
        except Exception as exc:  # noqa: BLE001 - only an optimisation
            log.info("model warm-up failed: %s", exc.__class__.__name__)
            return
        log.info("model warm-up done in %.0fs", self.clock.mono() - t0)

    async def stop(self) -> None:
        tasks = list(self.turns.values()) + list(self.background)
        if self.scheduler_task is not None:
            tasks.append(self.scheduler_task)
        for t in tasks:
            t.cancel()
        self.state.kv_set("clean_shutdown", "1")
        if self.tg_user_client is not None:
            with contextlib.suppress(Exception):
                await self.tg_user_client.aclose()
        with contextlib.suppress(Exception, asyncio.CancelledError):
            await asyncio.shield(self.hub.close())

    def seed_schedules(self) -> None:
        now = self.clock.wall()
        known = {s["name"]: s for s in self.state.schedules()}
        for s in self.config.schedule:
            nxt_dt = Cadence.parse(s.cadence, now=now, tz=self.tz).next_after(now, self.tz)
            nxt = nxt_dt.isoformat() if nxt_dt is not None else None
            cur = known.get(s.name)
            self.state.schedule_upsert(
                s.name,
                s.cadence,
                s.prompt,
                bool(cur["enabled"]) if cur else s.enabled,
                cur["next_run"] if cur and cur["cadence"] == s.cadence else nxt,
                "config",
            )

    # ---------------------------------------------------------- dispatching

    async def handle(self, update: Message | Callback) -> None:
        if isinstance(update, Callback):
            await self.on_callback(update)
            return
        age = self.clock.wall() - update.date
        if age > timedelta(minutes=self.config.telegram.offline_max_age_min):
            await self.channel.send(
                update.chat_id,
                "I was offline when you sent: "
                f"“{update.text[:80]}”. Please resend if it's still needed.",
            )
            return
        text = update.text.strip()
        if await knowme_runtime.finish_edit(self, update.chat_id, text):
            return
        if text.startswith("/"):
            await commands.dispatch(self, update.chat_id, text)
            return
        if self.paused:
            await self.channel.send(update.chat_id, "⏸ I'm stopped. Send /resume to continue.")
            return
        chat = update.chat_id
        waiting = self.pending.get(chat)
        if waiting is not None:  # a turn is already queued: this message joins it
            if text:
                waiting.append(text)
            self.pending_files.setdefault(chat, []).extend(update.attachments)
            return
        self.pending[chat] = [text] if text else []
        self.pending_files[chat] = list(update.attachments)
        running = self.turns.get(chat)
        if running is not None and not running.done():
            await self.channel.send(
                chat,
                "⏳ Still working on your previous message; "
                "I'll take this one when it's done. (/stop cancels)",
            )

        async def after(prev: asyncio.Task[Any] | None) -> None:
            if prev is not None and not prev.done():
                with contextlib.suppress(BaseException):
                    await prev
            # a long paste arrives as several messages within a second or two: one turn for all
            await asyncio.sleep(self.coalesce_s)
            texts = self.pending.pop(chat, [])
            files = self.pending_files.pop(chat, [])
            if (texts or files) and not self.paused:
                await self.chat_turn(chat, "\n\n".join(texts), "chat", attachments=files)

        # never block the dispatcher: approvals arrive through it while a turn waits
        self.turns[chat] = asyncio.create_task(after(running))

    def conversation(self, chat_id: int) -> str:
        conv = self.state.conversation_current(chat_id)
        if conv is None:
            conv = self.state.conversation_new(chat_id, self.clock.wall().isoformat())
        return str(conv["id"])

    async def chat_turn(
        self,
        chat_id: int,
        text: str,
        origin: str,
        conversation: str | None = None,
        attachments: list[Attachment] | None = None,
    ) -> str:
        cid = conversation or self.conversation(chat_id)
        await self.channel.typing(chat_id)
        images: list[ImageBlock] = []
        attached = ""
        note = ""
        if attachments:
            images, attached, note, problem = await self.receive_files(chat_id, attachments)
            if problem:
                await self.channel.send(chat_id, problem)
            if not images and not attached and not text:
                return ""

        indicator = _WaitIndicator(
            self.channel,
            chat_id,
            delay_s=self.wait_indicator_delay_s,
            interval_s=self.wait_indicator_interval_s,
        )
        await indicator.start()

        async def progress(status: str = "") -> None:
            if status:
                indicator.set_status(status)
            with contextlib.suppress(Exception):
                await self.channel.typing(chat_id)

        try:
            self.budget.check_day()
        except BudgetExceeded as exc:
            await indicator.stop()
            await self.channel.send(chat_id, f"⛔ {exc}. Raise the limit or wait until tomorrow.")
            return ""
        try:
            result = await self.agent.run_turn(
                cid,
                text,
                origin=origin,
                chat_id=chat_id,
                on_progress=progress,
                images=images,
                attached=attached,
                note=note,
            )
        except asyncio.CancelledError:
            await indicator.stop()
            with contextlib.suppress(Exception):
                await self.channel.send(chat_id, "⏹ Cancelled.")
            raise
        finally:
            await indicator.stop()

        reply = result.text
        if origin == "chat" and self.state.operator_turn(cid) == 1 and self.memory.available:
            reply += knowme_runtime.maybe_offer_interview(
                self, len(self.memory.facts(include_sensitive=False))
            )
        await self.channel.send(chat_id, reply, markdown=True)
        return result.text

    # --------------------------------------------------------------- models & tools

    def schedule_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "schedule__add",
                "Add a reminder, recurring schedule, or cron job to run on this laptop. "
                "Cadence options: 'in 15m' / 'in 2h' (relative timer), 'at 14:00' (time today/tomorrow), "
                "'daily HH:MM', 'weekdays HH:MM', 'every 30m', 'every 2h', or standard cron 'cron */15 * * * *'. "
                "Prompt is the reminder message or instruction to run.",
                {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "Identifier slug (e.g. 'remind_check_db', 'daily_brief')",
                        },
                        "cadence": {
                            "type": "string",
                            "description": "Cadence: 'in 15m', 'at 14:30', 'daily 08:00', 'cron 0 9 * * 1', etc.",
                        },
                        "prompt": {
                            "type": "string",
                            "description": "The reminder text or task to execute",
                        },
                    },
                    "required": ["name", "cadence", "prompt"],
                },
            ),
            ToolSpec(
                "schedule__list",
                "List all active and paused reminders, schedules, and cron jobs on this laptop.",
                {"type": "object", "properties": {}},
            ),
            ToolSpec(
                "schedule__remove",
                "Remove / cancel a scheduled reminder or cron job by name.",
                {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name of the schedule to remove",
                        }
                    },
                    "required": ["name"],
                },
            ),
            ToolSpec(
                "schedule__pause",
                "Pause a scheduled reminder or cron job by name.",
                {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name of the schedule to pause",
                        }
                    },
                    "required": ["name"],
                },
            ),
            ToolSpec(
                "schedule__resume",
                "Resume a paused schedule or cron job by name.",
                {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "The name of the schedule to resume",
                        }
                    },
                    "required": ["name"],
                },
            ),
        ]

    def files_specs(self) -> list[ToolSpec]:
        specs = [
            ToolSpec(
                "files__read",
                "Read text content of a file on this laptop. Supports ~, absolute, or relative paths. "
                "Returns line-numbered content with total lines.",
                {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to the file, e.g. '~/Documents/report.txt' or 'config.toml'",
                        },
                        "start_line": {
                            "type": "integer",
                            "description": "1-based start line number (default 1)",
                        },
                        "end_line": {
                            "type": "integer",
                            "description": "1-based end line number (default 500)",
                        },
                    },
                    "required": ["path"],
                },
            ),
            ToolSpec(
                "files__write",
                "Create or overwrite a file on this laptop. Automatically creates parent directories.",
                {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to file, e.g. '~/notes/summary.md'",
                        },
                        "content": {
                            "type": "string",
                            "description": "The full text content to write",
                        },
                        "overwrite": {
                            "type": "boolean",
                            "description": "Set to true to overwrite if file already exists (default false)",
                        },
                    },
                    "required": ["path", "content"],
                },
            ),
            ToolSpec(
                "files__edit",
                "Find and replace target text in a file on this laptop.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to file"},
                        "target": {"type": "string", "description": "Exact text to find"},
                        "replacement": {
                            "type": "string",
                            "description": "Text to replace target with",
                        },
                    },
                    "required": ["path", "target", "replacement"],
                },
            ),
            ToolSpec(
                "files__list",
                "List files and directories at a path on this laptop. Returns names, types, sizes, and modified dates.",
                {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Directory path (default '~')",
                        },
                        "max_depth": {
                            "type": "integer",
                            "description": "Directory depth (1 to 3, default 1)",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Glob filter pattern, e.g. '*.py' or '*.json' (default '*')",
                        },
                        "include_hidden": {
                            "type": "boolean",
                            "description": "Include hidden files starting with . (default false)",
                        },
                    },
                },
            ),
            ToolSpec(
                "files__search",
                "Search for files by name glob and/or grep inside file contents on this laptop.",
                {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Base directory to search (default '~')",
                        },
                        "query": {
                            "type": "string",
                            "description": "Text query to grep for inside files",
                        },
                        "pattern": {
                            "type": "string",
                            "description": "Filename glob pattern, e.g. '*.md' (default '*')",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of matches to return (default 25)",
                        },
                    },
                },
            ),
            ToolSpec(
                "files__info",
                "Get detailed metadata about a file or directory (size, modified time, exists, type).",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to inspect"},
                    },
                    "required": ["path"],
                },
            ),
            ToolSpec(
                "files__delete",
                "Delete a file or empty directory on this laptop.",
                {
                    "type": "object",
                    "properties": {
                        "path": {"type": "string", "description": "Path to delete"},
                    },
                    "required": ["path"],
                },
            ),
            ToolSpec(
                "files__send",
                "Send a document, PDF, image, or file from this laptop directly to the user's Telegram chat.",
                {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Path to file on laptop to send, e.g. '~/Downloads/book.pdf'",
                        },
                        "caption": {
                            "type": "string",
                            "description": "Optional caption for the file in Telegram",
                        },
                    },
                    "required": ["path"],
                },
            ),
        ]
        for t in specs:
            if t.name.removeprefix("files__") in FILE_CHANGES:
                t.description += " Needs the operator's approval (they see a preview first)."
        return specs

    def switch_spec(self) -> ToolSpec:
        assert self.router is not None
        names = list(self.router.profiles)
        return ToolSpec(
            "assistant__switch_model",
            "Ask the operator to switch the language model you run on. Use only when the "
            "operator asks for it. They confirm on their phone; it applies from the next "
            "message. Models: "
            + "; ".join(f"{a} = {m.name} ({m.where})" for a, m in self.router.profiles.items()),
            {
                "type": "object",
                "properties": {"model": {"type": "string", "enum": names}},
                "required": ["model"],
            },
        )

    def calendar_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "calendar__events",
                "List upcoming events and meetings from Google Calendar within a time window.",
                {
                    "type": "object",
                    "properties": {
                        "time_min": {
                            "type": "string",
                            "description": "Start datetime in ISO-8601 (e.g. 2026-11-12T00:00:00Z) or YYYY-MM-DD. Defaults to today.",
                        },
                        "time_max": {
                            "type": "string",
                            "description": "End datetime in ISO-8601 or YYYY-MM-DD. Defaults to +7 days.",
                        },
                        "query": {
                            "type": "string",
                            "description": "Optional search term to filter events by title, description or attendee.",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of events to return (default 10, max 50).",
                        },
                    },
                },
            ),
            ToolSpec(
                "calendar__create",
                "Create a new event in Google Calendar.",
                {
                    "type": "object",
                    "properties": {
                        "summary": {
                            "type": "string",
                            "description": "Title of the event or meeting.",
                        },
                        "start": {
                            "type": "string",
                            "description": "Start time in ISO-8601 (e.g. 2026-11-13T10:00:00+03:30) or YYYY-MM-DD.",
                        },
                        "end": {
                            "type": "string",
                            "description": "End time in ISO-8601 or YYYY-MM-DD.",
                        },
                        "description": {
                            "type": "string",
                            "description": "Optional notes or agenda for the meeting.",
                        },
                        "location": {
                            "type": "string",
                            "description": "Optional physical location or video meeting link.",
                        },
                        "attendees": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Optional list of attendee email addresses.",
                        },
                    },
                    "required": ["summary", "start", "end"],
                },
            ),
            ToolSpec(
                "calendar__delete",
                "Delete an event from Google Calendar. Needs the operator's approval.",
                {
                    "type": "object",
                    "properties": {
                        "event_id": {
                            "type": "string",
                            "description": "The unique Google Calendar event ID to delete.",
                        },
                    },
                    "required": ["event_id"],
                },
            ),
        ]

    def email_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "email__inbox",
                "List recent or unread emails from Gmail inbox.",
                {
                    "type": "object",
                    "properties": {
                        "unread_only": {
                            "type": "boolean",
                            "description": "Only return unread emails (default true).",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of messages to return (default 5, max 20).",
                        },
                    },
                },
            ),
            ToolSpec(
                "email__search",
                "Search Gmail messages using query syntax (e.g. from:x, subject:y, is:unread, after:2026-11-01).",
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Gmail search query string.",
                        },
                        "max_results": {
                            "type": "integer",
                            "description": "Maximum number of messages to return (default 5, max 20).",
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                "email__read",
                "Read the full body of an email message safely.",
                {
                    "type": "object",
                    "properties": {
                        "message_id": {
                            "type": "string",
                            "description": "The unique Gmail message ID to read.",
                        },
                    },
                    "required": ["message_id"],
                },
            ),
            ToolSpec(
                "email__draft",
                "Create a new email draft in Gmail without sending it.",
                {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email address."},
                        "subject": {"type": "string", "description": "Subject of the email."},
                        "body": {"type": "string", "description": "Body text of the email."},
                        "thread_id": {
                            "type": "string",
                            "description": "Optional thread ID to draft a reply in thread.",
                        },
                    },
                    "required": ["to", "subject", "body"],
                },
            ),
            ToolSpec(
                "email__send",
                "Send an email via Gmail. Needs the operator's approval.",
                {
                    "type": "object",
                    "properties": {
                        "to": {"type": "string", "description": "Recipient email address."},
                        "subject": {"type": "string", "description": "Subject of the email."},
                        "body": {"type": "string", "description": "Body text of the email."},
                        "thread_id": {
                            "type": "string",
                            "description": "Optional thread ID to reply in thread.",
                        },
                    },
                    "required": ["to", "subject", "body"],
                },
            ),
        ]

    def tg_user_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "tg_user__inbox",
                "List dialogs, unread messages, or find chats/groups/channels by title via optional query.",
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Optional search string to find a chat, group, or channel by title.",
                        },
                        "unread_only": {
                            "type": "boolean",
                            "description": "Only return dialogs with unread messages (default true, automatically false if query is provided).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of dialogs to return (default 10, max 30).",
                        },
                    },
                },
            ),
            ToolSpec(
                "tg_user__search",
                "Search personal Telegram messages and find matching chat/group/channel titles.",
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Keyword, phrase, or chat title to search for.",
                        },
                        "chat": {
                            "type": "string",
                            "description": "Target chat username, title, ID, or 'me' for Saved Messages. If omitted, searches globally.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Maximum number of matching messages (default 10, max 30).",
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                "tg_user__read",
                "Read recent message history from a specific personal chat, group, or channel.",
                {
                    "type": "object",
                    "properties": {
                        "chat": {
                            "type": "string",
                            "description": "Chat/group title (e.g. 'قلمرو شیطانی همدان'), username (@username), phone number, chat ID, or 'me' for Saved Messages.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of recent messages to retrieve (default 15, max 50).",
                        },
                    },
                    "required": ["chat"],
                },
            ),
            ToolSpec(
                "tg_user__send",
                "Send a message to a person, group, channel, or Saved Messages from your personal Telegram account. Needs the operator's approval.",
                {
                    "type": "object",
                    "properties": {
                        "recipient": {
                            "type": "string",
                            "description": "Group/channel/user title (e.g. 'قلمرو شیطانی همدان'), username (@user), phone number, chat ID, or 'me'.",
                        },
                        "message": {
                            "type": "string",
                            "description": "The exact text message to send.",
                        },
                    },
                    "required": ["recipient", "message"],
                },
            ),
            ToolSpec(
                "tg_user__save",
                "Save a note, snippet, or reminder to your personal Telegram 'Saved Messages' chat.",
                {
                    "type": "object",
                    "properties": {
                        "text": {
                            "type": "string",
                            "description": "Content to store in Saved Messages.",
                        },
                    },
                    "required": ["text"],
                },
            ),
        ]

    def kb_specs(self) -> list[ToolSpec]:
        return [
            ToolSpec(
                "kb__search",
                "Search passages across books, research papers, and PDFs in the Knowledge Base using semantic similarity.",
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "Natural language search query.",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of top matching passages to return (default 5, max 20).",
                        },
                        "document": {
                            "type": "string",
                            "description": "Optional book/paper title, filename, or document ID to restrict search to.",
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                "kb__ask",
                "Retrieve relevant context from books and research papers and formulate a grounded answer with citations.",
                {
                    "type": "object",
                    "properties": {
                        "query": {
                            "type": "string",
                            "description": "The specific question to answer based on the Knowledge Base.",
                        },
                        "document": {
                            "type": "string",
                            "description": "Optional document title or ID to restrict context to.",
                        },
                        "top_k": {
                            "type": "integer",
                            "description": "Number of passages to retrieve (default 5).",
                        },
                    },
                    "required": ["query"],
                },
            ),
            ToolSpec(
                "kb__list",
                "List all books, research papers, and PDFs currently indexed in the Knowledge Base.",
                {
                    "type": "object",
                    "properties": {},
                },
            ),
            ToolSpec(
                "kb__read",
                "Read sequential text chunks or sections from an indexed book, paper, or document.",
                {
                    "type": "object",
                    "properties": {
                        "document": {
                            "type": "string",
                            "description": "Document title, filename, or ID to read.",
                        },
                        "chunk_index": {
                            "type": "integer",
                            "description": "Starting chunk index (default 0).",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Number of consecutive chunks to return (default 3, max 10).",
                        },
                    },
                    "required": ["document"],
                },
            ),
            ToolSpec(
                "kb__add",
                "Ingest a new book, research paper, or PDF file into the Knowledge Base.",
                {
                    "type": "object",
                    "properties": {
                        "path": {
                            "type": "string",
                            "description": "Filesystem path to the PDF, DOCX, TXT, or Markdown document.",
                        },
                        "title": {
                            "type": "string",
                            "description": "Optional title for the document.",
                        },
                        "author": {
                            "type": "string",
                            "description": "Optional author name.",
                        },
                    },
                    "required": ["path"],
                },
            ),
            ToolSpec(
                "kb__remove",
                "Remove an indexed document and all its chunks from the Knowledge Base. Needs the operator's approval.",
                {
                    "type": "object",
                    "properties": {
                        "document": {
                            "type": "string",
                            "description": "Document title, filename, or ID to delete.",
                        },
                    },
                    "required": ["document"],
                },
            ),
        ]

    async def builtin(
        self,
        name: str,
        args: dict[str, Any],
        chat_id: int,
        conversation: str = "",
        origin: str = "chat",
    ) -> tuple[str, bool]:
        if name.startswith("me__"):
            return await knowme_runtime.call(self, name, args, chat_id, conversation, origin)
        if name.startswith("web__"):
            return await self.web_tool(name, args)
        if name.startswith("calendar__"):
            return await self.calendar_tool(name, args, chat_id, conversation, origin)
        if name.startswith("email__"):
            return await self.email_tool(name, args, chat_id, conversation, origin)
        if name.startswith("tg_user__"):
            return await self.tg_user_tool(name, args, chat_id, conversation, origin)
        if name.startswith("kb__"):
            return await self.kb_tool(name, args, chat_id, conversation, origin)
        if name == "assistant__switch_model":
            if self.router is None:
                return "no router configured", True
            alias = str(args.get("model", ""))
            if alias not in self.router.profiles:
                return f"unknown model '{alias}'; choose one of {list(self.router.profiles)}", True
            if alias == self.router.current:
                return f"already running on {alias}", False
            m = self.router.profiles[alias]
            await self.channel.send(
                chat_id,
                f"🔀 Switch the model to {alias} ({m.name}, {m.where})?",
                [
                    (f"Switch to {alias}", self.signer.sign(f"md:{alias}")),
                    (f"Keep {self.router.current}", self.signer.sign("md:")),
                ],
            )
            return "asked the operator to confirm the switch on their phone", False

        if name == "schedule__add":
            s_name = str(args.get("name", "")).strip()
            cadence = str(args.get("cadence", "")).strip()
            prompt = str(args.get("prompt", "")).strip()
            if not s_name or not cadence or not prompt:
                return "name, cadence, and prompt are all required", True
            try:
                now = self.clock.wall()
                cad = Cadence.parse(cadence, now=now, tz=self.tz)
                nxt = cad.next_after(now, self.tz)
                if nxt is None:
                    return f"cadence '{cadence}' has no upcoming execution", True
            except Exception as exc:  # noqa: BLE001
                return f"invalid cadence: {exc}", True
            cad_stored = f"once {nxt.isoformat()}" if cad.kind == "once" else cadence
            self.state.schedule_upsert(
                s_name, cad_stored, prompt[:2000], True, nxt.isoformat(), "agent"
            )
            self.state.schedule_set(s_name, enabled=1, next_run=nxt.isoformat())
            return (
                json.dumps(
                    {
                        "ok": True,
                        "name": s_name,
                        "cadence": cadence,
                        "next_run": nxt.isoformat(),
                        "prompt": prompt,
                    }
                ),
                False,
            )

        if name == "schedule__list":
            rows = self.state.schedules()
            return (
                json.dumps(
                    {
                        "ok": True,
                        "schedules": [
                            {
                                "name": r["name"],
                                "cadence": r["cadence"],
                                "prompt": r["prompt"],
                                "enabled": bool(r["enabled"]),
                                "next_run": r["next_run"],
                                "last_run": r.get("last_run"),
                            }
                            for r in rows
                        ],
                    }
                ),
                False,
            )

        if name == "schedule__remove":
            s_name = str(args.get("name", "")).strip()
            ok = self.state.schedule_remove(s_name)
            if ok:
                return json.dumps({"ok": True, "removed": s_name}), False
            return json.dumps({"ok": False, "error": f"no schedule named '{s_name}'"}), True

        if name == "schedule__pause":
            s_name = str(args.get("name", "")).strip()
            names = {s["name"] for s in self.state.schedules()}
            if s_name not in names:
                return json.dumps({"ok": False, "error": f"no schedule named '{s_name}'"}), True
            self.state.schedule_set(s_name, enabled=0)
            return json.dumps({"ok": True, "name": s_name, "paused": True}), False

        if name == "schedule__resume":
            s_name = str(args.get("name", "")).strip()
            sched = next((s for s in self.state.schedules() if s["name"] == s_name), None)
            if sched is None:
                return json.dumps({"ok": False, "error": f"no schedule named '{s_name}'"}), True
            now = self.clock.wall()
            try:
                cad = Cadence.parse(sched["cadence"], now=now, tz=self.tz)
                nxt = cad.next_after(now, self.tz)
                if nxt is None:
                    return (
                        json.dumps({"ok": False, "error": "one-time reminder has already expired"}),
                        True,
                    )
            except Exception as exc:  # noqa: BLE001
                return json.dumps({"ok": False, "error": str(exc)}), True
            self.state.schedule_set(s_name, enabled=1, next_run=nxt.isoformat())
            return (
                json.dumps(
                    {
                        "ok": True,
                        "name": s_name,
                        "resumed": True,
                        "next_run": nxt.isoformat(),
                    }
                ),
                False,
            )

        if name.startswith("files__"):
            tool_op = name.removeprefix("files__")
            if tool_op in FILE_CHANGES:
                denied = await self.approve_file_op(tool_op, args, chat_id, conversation, origin)
                if denied is not None:
                    return json.dumps(denied), True
            if tool_op == "read":
                res = files.files_read(
                    str(args.get("path", "")),
                    int(args.get("start_line", 1) or 1),
                    int(
                        args.get("end_line", files.DEFAULT_SLICE_LINES) or files.DEFAULT_SLICE_LINES
                    ),
                )
            elif tool_op == "write":
                res = files.files_write(
                    str(args.get("path", "")),
                    str(args.get("content", "")),
                    bool(args.get("overwrite", False)),
                )
            elif tool_op == "edit":
                res = files.files_edit(
                    str(args.get("path", "")),
                    str(args.get("target", "")),
                    str(args.get("replacement", "")),
                )
            elif tool_op == "list":
                res = await asyncio.to_thread(
                    files.files_list,
                    str(args.get("path", "~")),
                    int(args.get("max_depth", 1) or 1),
                    str(args.get("pattern", "*") or "*"),
                    bool(args.get("include_hidden", False)),
                )
            elif tool_op == "search":
                res = await asyncio.to_thread(
                    files.files_search,
                    str(args.get("path", "~")),
                    str(args.get("query", "")),
                    str(args.get("pattern", "*") or "*"),
                    int(args.get("max_results", 25) or 25),
                )
            elif tool_op == "info":
                res = files.files_info(str(args.get("path", "")))
            elif tool_op == "delete":
                res = files.files_delete(str(args.get("path", "")))
            elif tool_op == "send":
                p = files.resolve_path(str(args.get("path", "")))
                caption = str(args.get("caption", ""))
                try:
                    mid = await self.channel.send_document(chat_id, p, caption)
                    res = {"ok": True, "path": str(p), "message_id": mid, "sent": True}
                except Exception as exc:  # noqa: BLE001
                    res = {"ok": False, "error": str(exc)}
            else:
                return f"unknown files operation '{tool_op}'", True
            return json.dumps(res), not res.get("ok", False)

        return f"unknown tool {name}", True

    async def approve_file_op(
        self, op: str, args: dict[str, Any], chat_id: int, conversation: str, origin: str
    ) -> dict[str, Any] | None:
        """Changing or sending a file needs the operator's tap, like any state-changing tool.

        Returns None when approved, else the refusal to give the model."""
        target = files.resolve_path(str(args.get("path", "")))
        if (no := files.refused(target)) is not None:
            return no
        preview = file_preview(op, target, args)
        shown = {k: self.hub.mask(json.dumps(v, default=str))[:300] for k, v in args.items()}
        outcome = await self.approvals.request(
            chat_id=chat_id,
            conversation=conversation,
            server="files",
            tool=op,
            args=args,
            shown_args=shown,
            preview=preview,
            origin=origin,
        )
        if outcome.status != "approved":
            return {"ok": False, "error": f"not done: {outcome.status} by the operator"}
        if not self.approvals.bound(outcome.approval_id, "files", op, args):
            return {"ok": False, "error": "approval does not match the call"}
        self.approvals.mark_executed(outcome.approval_id)
        return None

    def switch_model(self, alias: str) -> str:
        """Switch now; returns the message for the operator."""
        if self.router is None:
            return "Only one model is configured."
        try:
            m = self.router.switch(alias)
            if m.local:
                self.spawn(self.warm_up(alias))
        except KeyError:
            return f"Unknown model '{alias}'. Models: {', '.join(self.router.profiles)}"
        except SecretMissing as exc:
            return f"⚠️ Can't switch to {alias}: {exc}"
        except Exception as exc:  # noqa: BLE001 - provider construction failed
            return f"⚠️ Can't switch to {alias}: {exc.__class__.__name__}"
        self.budget.prices = m.prices
        self.state.kv_set("model", alias)
        self.state.audit("system", origin="chat", target=f"model {alias}", outcome="switched")
        return f"🔀 Now using {alias}: {m.name} ({m.where})."

    # ------------------------------------------------------------ callbacks

    async def on_callback(self, cb: Callback) -> None:
        if cb.data.startswith("ap:"):
            await self.approvals.on_callback(cb.data, cb.id)
            return
        payload = self.signer.verify(cb.data)
        if payload is None:
            await self.channel.answer_callback(cb.id, "expired or invalid")
            return
        kind, _, rest = payload.partition(":")
        if kind == "pr":
            await self.on_proposal(cb, rest)
        elif kind == "ms":
            await self.channel.answer_callback(cb.id, "running")
            await self.run_schedule(rest, missed=True)
        elif kind == "md":
            text = self.switch_model(rest) if rest else "👍 Kept the current model."
            await self.channel.answer_callback(cb.id, "ok")
            await self.channel.edit(cb.chat_id, cb.message_id, text)
        elif kind == "un":
            text = await knowme_runtime.on_undo(self, rest)
            await self.channel.answer_callback(cb.id, "ok")
            await self.channel.edit(cb.chat_id, cb.message_id, text)
        elif kind == "fg1":
            text, buttons = await knowme_runtime.on_forget(self, rest)
            await self.channel.answer_callback(cb.id, "ok")
            await self.channel.edit(cb.chat_id, cb.message_id, text, buttons or None)
        elif kind == "ed":
            await self.channel.answer_callback(cb.id, "ok")
            await self.channel.send(cb.chat_id, knowme_runtime.start_edit(self, cb.chat_id, rest))
        elif kind == "pa":
            await self.on_save_all(cb, rest)
        elif kind == "fg":
            n = self.state.forget_all()
            await self.channel.answer_callback(cb.id, "forgotten")
            await self.channel.edit(cb.chat_id, cb.message_id, f"🧹 Forgot {n} conversation(s).")
        else:
            await self.channel.answer_callback(cb.id, "unknown")

    # ------------------------------------------------------------ media (feature 010)

    def vision_ok(self) -> tuple[bool, str]:
        """Whether the current model sees images; else the first profile that does."""
        if self.router is None:
            return self.config.model.vision, ""
        if self.router.profile.vision:
            return True, ""
        other = next((a for a, m in self.router.profiles.items() if m.vision), "")
        return False, other

    async def receive_files(
        self, chat_id: int, files_in: list[Attachment]
    ) -> tuple[list[ImageBlock], str, str, str]:
        """(images, document blocks for the model, note for history, problem to tell)."""
        images: list[ImageBlock] = []
        blocks: list[str] = []
        notes: list[str] = []
        problems: list[str] = []
        for f in files_in:
            label = "photo" if f.is_image else f"document {f.mime or f.name}"
            if f.size > media.MAX_DOWNLOAD:
                problems.append(
                    f"{f.name} is too large for me (Telegram lets bots download up to 20 MB)."
                )
                self.state.audit("media", target=label, outcome="refused: too large")
                continue
            if f.is_image:
                ok, other = self.vision_ok()
                if not ok:
                    cur = self.router.current if self.router else "current"
                    hint = f" /model {other} can." if other else ""
                    problems.append(f"My current model ({cur}) can't see images.{hint}")
                    self.state.audit("media", target=label, outcome="refused: no vision")
                    continue
            try:
                data = await self.channel.download(f.file_id)
            except Exception as exc:  # noqa: BLE001 - network or size
                problems.append(f"I couldn't download {f.name}.")
                self.state.audit("media", target=label, outcome=f"failed: {exc.__class__.__name__}")
                continue
            if f.is_image:
                images.append(ImageBlock(media.image_type(data, f.mime), media.b64(data)))
                notes.append("[photo]")
                self.state.audit("media", target=label, outcome=f"ok {len(data)} bytes")
                continue
            text, pages, cut, problem = media.document_text(f.name, f.mime, data)
            if problem:
                problems.append(problem)
                self.state.audit("media", target=label, outcome=f"refused: {problem}"[:200])
                continue
            blocks.append(media.document_block(f.name, f.mime, text, pages, cut))
            notes.append(f"[file: {f.name}]")
            self.state.audit("media", target=label, outcome=f"ok {len(text)} chars")
        return images, "\n\n".join(blocks), " ".join(notes), " ".join(problems)

    async def web_tool(self, name: str, args: dict[str, Any]) -> tuple[str, bool]:
        """web__search / web__read as built-ins; results are untrusted data (FR-004)."""

        def envelope(content: Any, ok: bool = True) -> tuple[str, bool]:
            return json.dumps(
                {"kind": "tool_output", "server": "web", "untrusted": True, "content": content},
                ensure_ascii=False,
            ), not ok

        cfg = self.config.web
        try:
            if name == "web__search":
                key = self.secret(cfg.key_name)
                if not key:
                    msg = (
                        f"web search is not set up: the operator needs a {cfg.search_provider} "
                        "API key, stored with: security add-generic-password -U -s "
                        f"{self.config.secrets.service} -a {cfg.key_name} -w"
                    )
                    self.state.audit("web", target="search", outcome="not set up")
                    return envelope({"error": msg}, ok=False)
                res = await web.search(
                    str(args.get("query", "")),
                    key=key,
                    count=int(args.get("count") or cfg.max_results),
                    timeout=cfg.timeout_s,
                    provider=cfg.search_provider,
                    transport=self.web_transport,
                )
                self.state.audit("web", target="search", outcome=f"ok {len(res)} results")
                return envelope({"results": res})
            if name == "web__read":
                page = await web.read(
                    str(args.get("url", "")),
                    max_chars=cfg.read_chars,
                    timeout=cfg.timeout_s,
                    resolver=self.web_resolver,
                    transport=self.web_transport,
                )
                host = urlsplit(page["final_url"]).hostname or "?"
                self.state.audit(
                    "web", target=f"read {host}", outcome=f"ok {len(page['text'])} chars"
                )
                return envelope(page)
        except web.WebError as exc:
            self.state.audit("web", target=name.removeprefix("web__"), outcome=str(exc)[:200])
            return envelope({"error": str(exc)}, ok=False)
        return envelope({"error": f"unknown tool {name}"}, ok=False)

    def google_auth(self) -> google_auth.GoogleAuth:
        creds = google_auth.GoogleCredentials.load(self.config, self.secret)
        return google_auth.GoogleAuth(
            creds,
            transport=self.google_transport,
            timeout=self.config.google.timeout_s,
        )

    async def calendar_tool(
        self,
        name: str,
        args: dict[str, Any],
        chat_id: int,
        conversation: str,
        origin: str,
    ) -> tuple[str, bool]:
        def envelope(content: Any, ok: bool = True) -> tuple[str, bool]:
            return json.dumps(
                {
                    "kind": "tool_output",
                    "server": "calendar",
                    "untrusted": True,
                    "content": content,
                },
                ensure_ascii=False,
            ), not ok

        try:
            auth = self.google_auth()
            if name == "calendar__events":
                events = await calendar.list_events(
                    auth,
                    time_min=args.get("time_min"),
                    time_max=args.get("time_max"),
                    query=str(args.get("query", "")),
                    max_results=int(args.get("max_results") or self.config.google.max_events),
                    timezone=self.config.assistant.timezone,
                )
                self.state.audit("calendar", target="events", outcome=f"ok {len(events)} events")
                return envelope({"events": events})

            if name == "calendar__create":
                res = await calendar.create_event(
                    auth,
                    summary=str(args.get("summary", "")),
                    start=str(args.get("start", "")),
                    end=str(args.get("end", "")),
                    description=str(args.get("description", "")),
                    location=str(args.get("location", "")),
                    attendees=args.get("attendees"),
                    timezone=self.config.assistant.timezone,
                )
                self.state.audit("calendar", target="create", outcome=f"ok {res.get('id')}")
                return envelope(res)

            if name == "calendar__delete":
                event_id = str(args.get("event_id", ""))
                preview = f"Delete Google Calendar event:\nID: {event_id}"
                outcome = await self.approvals.request(
                    chat_id=chat_id,
                    conversation=conversation,
                    server="calendar",
                    tool="delete",
                    args=args,
                    shown_args=args,
                    preview=preview,
                    origin=origin,
                )
                if outcome.status != "approved":
                    return envelope(
                        {"ok": False, "error": f"not done: {outcome.status} by the operator"},
                        ok=False,
                    )
                if not self.approvals.bound(outcome.approval_id, "calendar", "delete", args):
                    return envelope(
                        {"ok": False, "error": "approval does not match the call"}, ok=False
                    )
                self.approvals.mark_executed(outcome.approval_id)

                res = await calendar.delete_event(auth, event_id)
                self.state.audit("calendar", target="delete", outcome="ok")
                return envelope(res)

        except (google_auth.GoogleAuthError, calendar.CalendarError) as exc:
            self.state.audit(
                "calendar", target=name.removeprefix("calendar__"), outcome=str(exc)[:200]
            )
            return envelope({"error": str(exc)}, ok=False)
        return envelope({"error": f"unknown tool {name}"}, ok=False)

    async def email_tool(
        self,
        name: str,
        args: dict[str, Any],
        chat_id: int,
        conversation: str,
        origin: str,
    ) -> tuple[str, bool]:
        def envelope(content: Any, ok: bool = True) -> tuple[str, bool]:
            return json.dumps(
                {"kind": "tool_output", "server": "email", "untrusted": True, "content": content},
                ensure_ascii=False,
            ), not ok

        try:
            auth = self.google_auth()
            if name == "email__inbox":
                msgs = await gmail.list_inbox(
                    auth,
                    unread_only=bool(args.get("unread_only", True)),
                    max_results=int(args.get("max_results") or self.config.google.max_emails),
                )
                self.state.audit("email", target="inbox", outcome=f"ok {len(msgs)} messages")
                return envelope({"messages": msgs})

            if name == "email__search":
                msgs = await gmail.search_emails(
                    auth,
                    query=str(args.get("query", "")),
                    max_results=int(args.get("max_results") or self.config.google.max_emails),
                )
                self.state.audit("email", target="search", outcome=f"ok {len(msgs)} messages")
                return envelope({"messages": msgs})

            if name == "email__read":
                msg = await gmail.read_email(auth, str(args.get("message_id", "")))
                self.state.audit(
                    "email", target="read", outcome=f"ok {len(msg.get('body', ''))} chars"
                )
                return envelope(msg)

            if name == "email__draft":
                res = await gmail.create_draft(
                    auth,
                    to=str(args.get("to", "")),
                    subject=str(args.get("subject", "")),
                    body=str(args.get("body", "")),
                    thread_id=args.get("thread_id"),
                )
                self.state.audit("email", target="draft", outcome=f"ok {res.get('draft_id')}")
                return envelope(res)

            if name == "email__send":
                to = str(args.get("to", ""))
                subject = str(args.get("subject", ""))
                body = str(args.get("body", ""))
                preview = f"Send email via Gmail:\nTo: {to}\nSubject: {subject}\n\n{body[:300]}"
                outcome = await self.approvals.request(
                    chat_id=chat_id,
                    conversation=conversation,
                    server="email",
                    tool="send",
                    args=args,
                    shown_args={"to": to, "subject": subject},
                    preview=preview,
                    origin=origin,
                )
                if outcome.status != "approved":
                    return envelope(
                        {"ok": False, "error": f"not done: {outcome.status} by the operator"},
                        ok=False,
                    )
                if not self.approvals.bound(outcome.approval_id, "email", "send", args):
                    return envelope(
                        {"ok": False, "error": "approval does not match the call"}, ok=False
                    )
                self.approvals.mark_executed(outcome.approval_id)

                res = await gmail.send_email(
                    auth,
                    to=to,
                    subject=subject,
                    body=body,
                    thread_id=args.get("thread_id"),
                )
                self.state.audit("email", target="send", outcome="ok")
                return envelope(res)

        except (google_auth.GoogleAuthError, gmail.GmailError) as exc:
            self.state.audit("email", target=name.removeprefix("email__"), outcome=str(exc)[:200])
            return envelope({"error": str(exc)}, ok=False)
        return envelope({"error": f"unknown tool {name}"}, ok=False)

    def telegram_user_client(self) -> telegram_user.TelegramUserClient:
        if self.tg_user_client is not None:
            return self.tg_user_client
        creds = telegram_user.TelegramCredentials.load(self.config, self.secret)
        self.tg_user_client = telegram_user.TelegramUserClient(creds)
        return self.tg_user_client

    async def tg_user_tool(
        self,
        name: str,
        args: dict[str, Any],
        chat_id: int,
        conversation: str,
        origin: str,
    ) -> tuple[str, bool]:
        def envelope(content: Any, ok: bool = True) -> tuple[str, bool]:
            return json.dumps(
                {
                    "kind": "tool_output",
                    "server": "telegram_user",
                    "untrusted": True,
                    "content": content,
                },
                ensure_ascii=False,
            ), not ok

        try:
            client = self.telegram_user_client()
            if name == "tg_user__inbox":
                query = str(args.get("query", "")).strip()
                unread_default = False if query else True
                unread_only = bool(args.get("unread_only", unread_default))
                limit = int(args.get("limit") or self.config.telegram_user.max_messages)
                dialogs = await client.list_inbox(unread_only=unread_only, query=query, limit=limit)
                self.state.audit(
                    "telegram_user", target="inbox", outcome=f"ok {len(dialogs)} dialogs"
                )
                return envelope({"dialogs": dialogs})

            if name == "tg_user__search":
                query = str(args.get("query", ""))
                chat = str(args.get("chat", ""))
                limit = int(args.get("limit") or self.config.telegram_user.max_messages)
                res = await client.search_messages(query=query, chat=chat, limit=limit)
                self.state.audit(
                    "telegram_user",
                    target="search",
                    outcome=f"ok {len(res.get('messages', []))} msgs, {len(res.get('chats', []))} chats",
                )
                return envelope(res)

            if name == "tg_user__read":
                chat = str(args.get("chat", ""))
                limit = int(args.get("limit") or self.config.telegram_user.max_messages)
                messages = await client.read_history(chat=chat, limit=limit)
                self.state.audit(
                    "telegram_user", target="read", outcome=f"ok {len(messages)} messages"
                )
                return envelope({"messages": messages})

            if name == "tg_user__save":
                text = str(args.get("text", ""))
                res = await client.save_to_saved(text=text)
                self.state.audit(
                    "telegram_user",
                    target="save",
                    outcome=f"ok msg {res.get('message_id')}",
                )
                return envelope(res)

            if name == "tg_user__send":
                recipient = str(args.get("recipient", ""))
                msg_text = str(args.get("message", ""))
                preview = (
                    f"Send Telegram message from personal account:\n"
                    f"To: {recipient}\n\n{msg_text[:300]}"
                )
                outcome = await self.approvals.request(
                    chat_id=chat_id,
                    conversation=conversation,
                    server="telegram_user",
                    tool="send",
                    args=args,
                    shown_args={"recipient": recipient, "message": msg_text[:80]},
                    preview=preview,
                    origin=origin,
                )
                if outcome.status != "approved":
                    return envelope(
                        {"ok": False, "error": f"not done: {outcome.status} by the operator"},
                        ok=False,
                    )
                if not self.approvals.bound(outcome.approval_id, "telegram_user", "send", args):
                    return envelope(
                        {"ok": False, "error": "approval does not match the call"}, ok=False
                    )
                self.approvals.mark_executed(outcome.approval_id)

                res = await client.send_message(recipient=recipient, text=msg_text)
                self.state.audit("telegram_user", target="send", outcome="ok")
                return envelope(res)

        except (telegram_user.TelegramUserError, Exception) as exc:
            self.state.audit(
                "telegram_user",
                target=name.removeprefix("tg_user__"),
                outcome=str(exc)[:200],
            )
            return envelope({"error": str(exc)}, ok=False)
        return envelope({"error": f"unknown tool {name}"}, ok=False)

    def knowledge_base_engine(self) -> knowledge_base.KnowledgeBase:
        if self.kb is not None:
            return self.kb
        self.kb = knowledge_base.KnowledgeBase(
            self.config.knowledge_base.storage_dir,
            chunk_size=self.config.knowledge_base.chunk_size,
            chunk_overlap=self.config.knowledge_base.chunk_overlap,
        )
        return self.kb

    async def kb_tool(
        self,
        name: str,
        args: dict[str, Any],
        chat_id: int,
        conversation: str,
        origin: str,
    ) -> tuple[str, bool]:
        def envelope(content: Any, ok: bool = True) -> tuple[str, bool]:
            return json.dumps(
                {
                    "kind": "tool_output",
                    "server": "knowledge_base",
                    "untrusted": True,
                    "content": content,
                },
                ensure_ascii=False,
            ), not ok

        try:
            kb = self.knowledge_base_engine()
            if name == "kb__search":
                query = str(args.get("query", ""))
                top_k = int(args.get("top_k") or self.config.knowledge_base.top_k)
                doc = str(args.get("document", ""))
                results = kb.search(query=query, top_k=top_k, document=doc)
                self.state.audit(
                    "knowledge_base", target="search", outcome=f"ok {len(results)} results"
                )
                return envelope({"results": results})

            if name == "kb__ask":
                query = str(args.get("query", ""))
                top_k = int(args.get("top_k") or self.config.knowledge_base.top_k)
                doc = str(args.get("document", ""))
                results = kb.search(query=query, top_k=top_k, document=doc)
                self.state.audit(
                    "knowledge_base", target="ask", outcome=f"ok {len(results)} context chunks"
                )
                return envelope(
                    {
                        "query": query,
                        "document_filter": doc or "all",
                        "retrieved_passages": results,
                    }
                )

            if name == "kb__list":
                docs = kb.list_documents()
                self.state.audit(
                    "knowledge_base", target="list", outcome=f"ok {len(docs)} documents"
                )
                return envelope({"documents": docs})

            if name == "kb__read":
                doc = str(args.get("document", ""))
                chunk_index = int(args.get("chunk_index", 0))
                limit = int(args.get("limit", 3))
                chunks = kb.read_chunks(document=doc, chunk_index=chunk_index, limit=limit)
                self.state.audit(
                    "knowledge_base", target="read", outcome=f"ok {len(chunks)} chunks"
                )
                return envelope({"chunks": chunks})

            if name == "kb__add":
                path_str = str(args.get("path", ""))
                title = str(args.get("title", ""))
                author = str(args.get("author", ""))
                res = kb.ingest_file(path_str, title=title, author=author)
                self.state.audit(
                    "knowledge_base", target="add", outcome=f"ok doc {res.get('doc_id')}"
                )
                return envelope(res)

            if name == "kb__remove":
                doc = str(args.get("document", ""))
                preview = (
                    f"Delete document '{doc}' and all its vector embeddings from the Knowledge Base"
                )
                outcome = await self.approvals.request(
                    chat_id=chat_id,
                    conversation=conversation,
                    server="knowledge_base",
                    tool="remove",
                    args=args,
                    shown_args={"document": doc},
                    preview=preview,
                    origin=origin,
                )
                if outcome.status != "approved":
                    return envelope(
                        {"ok": False, "error": f"not done: {outcome.status} by the operator"},
                        ok=False,
                    )
                if not self.approvals.bound(outcome.approval_id, "knowledge_base", "remove", args):
                    return envelope(
                        {"ok": False, "error": "approval does not match the call"}, ok=False
                    )
                self.approvals.mark_executed(outcome.approval_id)

                res = kb.remove_document(doc)
                self.state.audit("knowledge_base", target="remove", outcome="ok")
                return envelope(res)

        except (knowledge_base.KnowledgeBaseError, Exception) as exc:
            self.state.audit(
                "knowledge_base",
                target=name.removeprefix("kb__"),
                outcome=str(exc)[:200],
            )
            return envelope({"error": str(exc)}, ok=False)
        return envelope({"error": f"unknown tool {name}"}, ok=False)

    def secret(self, name: str) -> str:
        from assistant.secrets import SecretMissing, provider_for

        try:
            prov = provider_for(
                self.config.secrets.provider, self.config.secrets.service, self.config.secrets.file
            )
            return prov.get(name)
        except SecretMissing:
            return ""

    def fresh_proposals(self, props: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Drop proposals that repeat a known fact or each other (feature 009, R6)."""
        from agent_memory.similarity import jaccard

        known = [f.content for f in self.memory.facts()] if self.memory.available else []
        out: list[dict[str, Any]] = []
        for p in props:
            text = str(p.get("content", ""))
            if any(jaccard(text, k) >= 0.8 for k in known + [str(o["content"]) for o in out]):
                continue
            out.append(p)
        return out[: max(self.config.limits.max_proposals, 1) + 3]

    async def on_save_all(self, cb: Callback, conversation: str) -> None:
        rows = self.state.proposals_pending(conversation)
        saved = 0
        for row in rows:
            try:
                res = self.memory.save(row["payload"], conversation)
            except Exception as exc:  # noqa: BLE001 - e.g. a secret: skip that one
                self.state.proposal_set(row["id"], "discarded")
                self.state.audit(
                    "memory_save",
                    conversation=conversation,
                    target=row["payload"]["subject"],
                    outcome=f"refused: {getattr(exc, 'message', exc.__class__.__name__)}"[:200],
                )
                continue
            self.state.proposal_set(row["id"], "saved")
            self.state.audit(
                "memory_save",
                conversation=conversation,
                target=row["payload"]["subject"],
                outcome=str(res.get("outcome")),
            )
            saved += 1
        await self.channel.answer_callback(cb.id, "saved")
        await self.channel.edit(cb.chat_id, cb.message_id, f"💾 Saved {saved} of {len(rows)}.")

    async def retry_recaps(self, now: datetime) -> None:
        """Recaps that failed (model unavailable) are retried hourly for a day (R5)."""
        for key, val in self.state.kv_prefix("recap_pending:").items():
            first, _, last = val.partition("|")
            cid = key.split(":", 1)[1]
            if first < (now - timedelta(hours=24)).isoformat():
                self.state.kv_del(key)
                continue
            if last > (now - timedelta(hours=1)).isoformat():
                continue
            conv = self.state.conversation_get(cid)
            if conv is None:
                self.state.kv_del(key)
                continue
            await self.end_and_propose(int(conv["chat_id"]), cid)

    async def on_proposal(self, cb: Callback, rest: str) -> None:
        pid, _, choice = rest.partition(":")
        row = self.state.proposal_get(pid)
        if row is None or row["status"] != "pending" or choice not in ("s", "d"):
            await self.channel.answer_callback(cb.id, "expired or already handled")
            return
        p = row["payload"]
        if choice == "d":
            self.state.proposal_set(pid, "discarded")
            await self.channel.answer_callback(cb.id, "discarded")
            await self.channel.edit(cb.chat_id, cb.message_id, f"🗑 Discarded: {p['title']}")
            return
        try:
            res = self.memory.save(p, row["conversation"])
            self.state.proposal_set(pid, "saved")
            self.state.audit(
                "memory_save",
                conversation=row["conversation"],
                target=p["subject"],
                outcome=res["outcome"],
            )
            note = {
                "created": "💾 Saved",
                "confirmed": "💾 Already known (confirmed)",
                "conflict": "⚠️ Saved, but it conflicts with an existing memory",
            }[res["outcome"]]
            await self.channel.edit(cb.chat_id, cb.message_id, f"{note}: {p['title']}")
            await self.channel.answer_callback(cb.id, "saved")
        except Exception as exc:  # noqa: BLE001 - agent-memory refused it (e.g. a secret)
            self.state.proposal_set(pid, "discarded")
            msg = getattr(exc, "message", exc.__class__.__name__)
            self.state.audit(
                "memory_save",
                conversation=row["conversation"],
                target=p["subject"],
                outcome=f"refused: {msg}"[:200],
            )
            await self.channel.edit(cb.chat_id, cb.message_id, f"⛔ Not saved: {msg}")
            await self.channel.answer_callback(cb.id, "refused")

    async def end_and_propose(self, chat_id: int, conversation: str) -> int:
        props = await self.agent.end_conversation(conversation)
        props = self.fresh_proposals(props + self.pending_suggestions.pop(conversation, []))
        for p in props:
            pid = new_id("p")
            self.state.proposal_add(pid, conversation, p)
            text = (
                f"💡 Remember this? [{p['type']}] {p['subject']}\n{p['title']}\n\n"
                f"{p['content']}"
                + (f"\n\nWhy: {p['why']}" if p.get("why") else "")
                + ("\n📌 Pinned: I'll see this in every conversation." if p.get("pin") else "")
            )
            await self.channel.send(
                chat_id,
                text,
                [
                    ("💾 Save", self.signer.sign(f"pr:{pid}:s")),
                    ("🗑 Discard", self.signer.sign(f"pr:{pid}:d")),
                ],
            )
        if len(props) > 1:
            await self.channel.send(
                chat_id,
                f"Or keep all {len(props)} at once:",
                [("💾 Save all", self.signer.sign(f"pa:{conversation}"))],
            )
        if self.agent.proposal_error:
            await self.channel.send(
                chat_id,
                "⚠️ I couldn't turn this conversation into memory proposals: "
                f"{self.agent.proposal_error}. Nothing was saved. Paste the important parts "
                "again and send /done to retry.",
            )
        return len(props)

    # ------------------------------------------------------------ schedules

    def notify_desktop(self, title: str, text: str) -> None:
        """Best-effort desktop notification on the laptop (macOS)."""
        if sys.platform != "darwin":
            return
        with contextlib.suppress(Exception):
            safe_title = title.replace("\\", "\\\\").replace('"', '\\"')
            safe_text = text.replace("\\", "\\\\").replace('"', '\\"')[:200]
            subprocess.run(
                [
                    "/usr/bin/osascript",
                    "-e",
                    f'display notification "{safe_text}" with title "{safe_title}"',
                ],
                check=False,
                capture_output=True,
                timeout=3,
            )

    async def run_schedule(self, name: str, missed: bool = False) -> None:
        sched = next((s for s in self.state.schedules() if s["name"] == name), None)
        if sched is None or self.paused:
            return
        now = self.clock.wall()
        self.state.schedule_set(name, last_run=now.isoformat())
        self.state.audit(
            "schedule_run", origin="schedule", target=name, outcome="missed-run" if missed else "ok"
        )
        self.notify_desktop(f"⏰ {name}", sched["prompt"])
        conv = self.state.conversation_new(self.owner_chat, now.isoformat())
        await self.channel.send(self.owner_chat, f"⏰ {name}")
        await self.chat_turn(self.owner_chat, sched["prompt"], "schedule", str(conv["id"]))
        self.state.conversation_end(str(conv["id"]))

    async def tick(self) -> None:
        """One scheduler pass: sleep detection, due schedules, heartbeat, idle conversations."""
        now = self.clock.wall()
        gap = self.sleep.check()
        schedules = [s for s in self.state.schedules() if s["enabled"]]
        if gap is not None:
            missed = [s for s in schedules if s["next_run"] and s["next_run"] <= now.isoformat()]
            for s in schedules:
                if s["next_run"] and s["next_run"] <= now.isoformat():
                    cad = Cadence.parse(s["cadence"], now=now, tz=self.tz)
                    nxt = cad.next_after(now, self.tz)
                    if nxt is None:
                        self.state.schedule_set(s["name"], enabled=0, next_run=None)
                    else:
                        self.state.schedule_set(s["name"], next_run=nxt.isoformat())
            if missed:
                names = ", ".join(s["name"] for s in missed)
                latest = max(missed, key=lambda s: str(s["next_run"]))
                await self.channel.send(
                    self.owner_chat,
                    f"😴 I was asleep and missed: {names}. Nothing was replayed.",
                    [(f"▶️ Run {latest['name']} now", self.signer.sign(f"ms:{latest['name']}"))],
                )
            return
        for s in schedules:
            if s["next_run"] and s["next_run"] <= now.isoformat():
                cad = Cadence.parse(s["cadence"], now=now, tz=self.tz)
                nxt = cad.next_after(now, self.tz)
                if nxt is None:
                    self.state.schedule_set(s["name"], enabled=0, next_run=None)
                else:
                    self.state.schedule_set(s["name"], next_run=nxt.isoformat())
                if not self.paused:
                    self.spawn(self.run_schedule(s["name"]))
        await self.heartbeat(now)
        await self.sweep_idle(now)

    async def heartbeat(self, now: datetime) -> None:
        cadence = self.config.assistant.heartbeat
        if not cadence:
            return
        nxt = self.state.kv_get("next_heartbeat")
        nxt_dt = Cadence.parse(cadence).next_after(now, self.tz)
        if nxt_dt is None:
            return
        nxt_iso = nxt_dt.isoformat()
        if nxt is None:
            self.state.kv_set("next_heartbeat", nxt_iso)
            return
        if nxt > now.isoformat():
            return
        self.state.kv_set("next_heartbeat", nxt_iso)
        self.state.kv_set("last_heartbeat", now.isoformat())
        up = now - self.started
        await self.channel.send(
            self.owner_chat,
            f"💓 Alive · up {int(up.total_seconds() // 3600)}h · {self.budget.usage_text()}",
        )

    async def sweep_idle(self, now: datetime) -> None:
        await self.retry_recaps(now)
        cutoff = (now - timedelta(minutes=self.config.limits.idle_minutes)).isoformat()
        for conv in self.state.idle_conversations(cutoff):
            if self.turns.get(int(conv["chat_id"])) and not self.turns[int(conv["chat_id"])].done():
                continue
            await self.end_and_propose(int(conv["chat_id"]), str(conv["id"]))

    # ----------------------------------------------------------- main loops

    async def scheduler_loop(self, interval: float = 30.0) -> None:
        while True:
            try:
                await self.tick()
            except Exception:  # noqa: BLE001 - the loop must survive
                log.exception("scheduler tick failed")
            await asyncio.sleep(interval)

    async def run(self) -> None:
        main = asyncio.current_task()
        if main is not None:  # launchd/systemd stop with SIGTERM: shut down cleanly, not a crash
            with contextlib.suppress(NotImplementedError, RuntimeError):
                asyncio.get_running_loop().add_signal_handler(signal.SIGTERM, main.cancel)
        await self.start()
        self.scheduler_task = asyncio.create_task(self.scheduler_loop())
        try:
            async for upd in self.channel.updates():
                try:
                    await self.handle(upd)
                except Exception:  # noqa: BLE001
                    log.exception("handling an update failed")
        finally:
            await self.stop()
