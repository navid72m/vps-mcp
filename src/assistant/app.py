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

from assistant import commands, files
from assistant.agent import Agent
from assistant.approvals import Approvals, Signer
from assistant.budget import Budget, BudgetExceeded
from assistant.clock import Clock, RealClock
from assistant.config import Config
from assistant.memory import AssistantMemory
from assistant.providers import Provider, ToolSpec
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
        if self.router is not None:
            self.budget.prices = self.router.profile.prices
            if len(self.router.profiles) > 1:
                specs.append(self.switch_spec())
        self.agent.builtin_specs = specs
        self.agent.builtin = self.builtin
        self.owner_chat = config.telegram.allowed_user_ids[0]
        self.turns: dict[int, asyncio.Task[Any]] = {}
        self.pending: dict[int, list[str]] = {}  # messages waiting for the next turn, per chat
        self.coalesce_s = coalesce_s
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
        if text.startswith("/"):
            await commands.dispatch(self, update.chat_id, text)
            return
        if self.paused:
            await self.channel.send(update.chat_id, "⏸ I'm stopped. Send /resume to continue.")
            return
        chat = update.chat_id
        waiting = self.pending.get(chat)
        if waiting is not None:  # a turn is already queued: this message joins it
            waiting.append(text)
            return
        self.pending[chat] = [text]
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
            if texts and not self.paused:
                await self.chat_turn(chat, "\n\n".join(texts), "chat")

        # never block the dispatcher: approvals arrive through it while a turn waits
        self.turns[chat] = asyncio.create_task(after(running))

    def conversation(self, chat_id: int) -> str:
        conv = self.state.conversation_current(chat_id)
        if conv is None:
            conv = self.state.conversation_new(chat_id, self.clock.wall().isoformat())
        return str(conv["id"])

    async def chat_turn(
        self, chat_id: int, text: str, origin: str, conversation: str | None = None
    ) -> str:
        cid = conversation or self.conversation(chat_id)
        await self.channel.typing(chat_id)

        async def progress() -> None:
            await self.channel.typing(chat_id)

        try:
            self.budget.check_day()
        except BudgetExceeded as exc:
            await self.channel.send(chat_id, f"⛔ {exc}. Raise the limit or wait until tomorrow.")
            return ""
        try:
            result = await self.agent.run_turn(
                cid, text, origin=origin, chat_id=chat_id, on_progress=progress
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self.channel.send(chat_id, "⏹ Cancelled.")
            raise
        await self.channel.send(chat_id, result.text, markdown=True)
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

    async def builtin(
        self,
        name: str,
        args: dict[str, Any],
        chat_id: int,
        conversation: str = "",
        origin: str = "chat",
    ) -> tuple[str, bool]:
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
        elif kind == "fg":
            n = self.state.forget_all()
            await self.channel.answer_callback(cb.id, "forgotten")
            await self.channel.edit(cb.chat_id, cb.message_id, f"🧹 Forgot {n} conversation(s).")
        else:
            await self.channel.answer_callback(cb.id, "unknown")

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
        for p in props:
            pid = new_id("p")
            self.state.proposal_add(pid, conversation, p)
            text = (
                f"💡 Remember this? [{p['type']}] {p['subject']}\n{p['title']}\n\n"
                f"{p['content']}" + (f"\n\nWhy: {p['why']}" if p.get("why") else "")
            )
            await self.channel.send(
                chat_id,
                text,
                [
                    ("💾 Save", self.signer.sign(f"pr:{pid}:s")),
                    ("🗑 Discard", self.signer.sign(f"pr:{pid}:d")),
                ],
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
