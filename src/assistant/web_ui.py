"""Web User Interface & Executive Cockpit for the Personal Assistant (Digital Secretary).

Provides an executive dashboard and REST API for:
- Secretary Console (interactive chat with LLM routing)
- Action Guardrail (human-in-the-loop approvals for sensitive tools)
- Agenda & Executive Desk (Google Calendar & Gmail inbox)
- Knowledge Base RAG (dense vector semantic search across books/PDFs with page citations)
- Infrastructure Telemetry (VPS host CPU/RAM/Disk metrics & managed services)
- Profile & Memory ("Knows Me" operator identity, facts, procedures)
- Scheduled Routines & Timers
- Immutable Security Audit Trail
"""

from __future__ import annotations

import asyncio
import datetime
import logging
import webbrowser
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, Response
from starlette.routing import Route

from assistant import calendar, gmail
from assistant import config as cfgmod
from assistant.google_auth import GoogleAuth, GoogleCredentials
from assistant.knowledge_base import KnowledgeBase
from assistant.memory import AssistantMemory
from assistant.providers import Msg, TextBlock
from assistant.secrets import provider_for
from assistant.state import State, now_iso

log = logging.getLogger("assistant.web_ui")
TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"


def _read_index_html() -> str:
    path = TEMPLATES_DIR / "index.html"
    if path.is_file():
        return path.read_text(encoding="utf-8")
    return "<html><body><h1>Digital Secretary UI</h1><p>Template not found.</p></body></html>"


class WebSecretaryApp:
    def __init__(self, config: cfgmod.Config | None = None) -> None:
        self.cfg = config or cfgmod.load()
        self.state = State(self.cfg.state_dir / "assistant.db")
        self.kb = KnowledgeBase(
            self.cfg.knowledge_base.storage_dir,
            chunk_size=self.cfg.knowledge_base.chunk_size,
            chunk_overlap=self.cfg.knowledge_base.chunk_overlap,
        )
        self.memory = AssistantMemory(self.cfg)
        self.secrets = provider_for(
            self.cfg.secrets.provider, self.cfg.secrets.service, self.cfg.secrets.file
        )
        self.vps_config_path = Path("~/.config/vps-mcp/jafarzadeh.toml").expanduser()
        self.vps_config_exists = self.vps_config_path.is_file()

    # ---------------------------------------------------------------- Google Auth
    def _get_google_auth(self) -> GoogleAuth | None:
        if not self.cfg.google.enabled:
            return None
        try:
            creds = GoogleCredentials.load(self.cfg, self.secrets.get)
            return GoogleAuth(creds)
        except Exception as exc:
            log.warning("Google Auth unavailable: %s", exc)
            return None

    # ---------------------------------------------------------------- Routes
    async def index(self, request: Request) -> Response:
        return HTMLResponse(_read_index_html())

    async def api_status(self, request: Request) -> JSONResponse:
        today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        budget = self.state.budget_get(today)
        pending_approvals = self.state.approvals_pending()
        current_model = self.state.kv_get("model", self.cfg.model.alias)
        facts_count = len(self.memory.facts()) if self.memory.available else 0
        docs_count = len(self.kb.list_documents())

        return JSONResponse(
            {
                "assistant_name": self.cfg.assistant.name,
                "timezone": self.cfg.assistant.timezone,
                "active_model": current_model,
                "budget_today": budget,
                "pending_approvals_count": len(pending_approvals),
                "memory_facts_count": facts_count,
                "kb_docs_count": docs_count,
                "time_utc": now_iso(),
            }
        )

    async def api_models(self, request: Request) -> JSONResponse:
        current_model = self.state.kv_get("model", self.cfg.model.alias)
        models = [
            {
                "alias": alias,
                "name": m.name,
                "provider": m.provider,
                "base_url": m.base_url,
                "timeout_s": m.timeout_s,
            }
            for alias, m in self.cfg.profiles.items()
        ]
        return JSONResponse({"active": current_model, "models": models})

    async def api_models_switch(self, request: Request) -> JSONResponse:
        body = await request.json()
        target = str(body.get("model", "")).strip()
        if target not in self.cfg.profiles:
            return JSONResponse({"error": f"Unknown model alias '{target}'"}, status_code=400)
        self.state.kv_set("model", target)
        self.state.audit("system", origin="web", outcome=f"switched model to {target}")
        return JSONResponse({"ok": True, "active": target})

    # ---------------------------------------------------------------- Chat
    async def api_chat(self, request: Request) -> JSONResponse:
        body = await request.json()
        message = str(body.get("message", "")).strip()
        if not message:
            return JSONResponse({"error": "Empty message"}, status_code=400)

        cid = str(body.get("cid", "")).strip()
        chat_id = self.cfg.telegram.allowed_user_ids[0] if self.cfg.telegram.allowed_user_ids else 1000

        if not cid:
            conv = self.state.conversation_current(chat_id)
            if not conv:
                conv = self.state.conversation_new(chat_id, now_iso())
            cid = str(conv["id"])

        now_ts = now_iso()
        self.state.add_turn(cid, now_ts, "user", message)
        self.state.conversation_touch(cid, now_ts)

        active_alias = self.state.kv_get("model", self.cfg.model.alias) or self.cfg.model.alias
        model_sec = self.cfg.profiles.get(active_alias, self.cfg.model)
        from assistant.cli import make_provider

        tools_used: list[str] = []
        assistant_reply = ""

        # Contextual tool pre-check (RAG, Calendar, Services, Memory)
        q_lower = message.lower()
        extra_context = ""

        try:
            if any(k in q_lower for k in ("ddia", "book", "paper", "lineariz", "isolation", "lsm", "b-tree")):
                kb_results = self.kb.search(message, top_k=3)
                if kb_results:
                    tools_used.append("kb__search")
                    citations = "\n".join(
                        f"• [{r['title']}, p. {r.get('page', '?')}]: {r['text'][:350]}"
                        for r in kb_results
                    )
                    extra_context += f"\n\n[Knowledge Base Passages]:\n{citations}"

            if any(k in q_lower for k in ("agenda", "calendar", "schedule today", "meeting", "events")):
                auth = self._get_google_auth()
                if auth:
                    evs = await calendar.list_events(auth, timezone=self.cfg.assistant.timezone, max_results=5)
                    tools_used.append("calendar__events")
                    ev_lines = "\n".join(f"• {e['summary']} ({e.get('time_span', e.get('start'))})" for e in evs)
                    extra_context += f"\n\n[Google Calendar Upcoming]:\n{ev_lines or 'No events scheduled.'}"

            if any(k in q_lower for k in ("email", "inbox", "mail", "unread")):
                auth = self._get_google_auth()
                if auth:
                    msgs = await gmail.list_inbox(auth, max_results=5)
                    tools_used.append("email__inbox")
                    m_lines = "\n".join(f"• From: {m.get('from')} | Subject: {m.get('subject')}" for m in msgs)
                    extra_context += f"\n\n[Gmail Recent Inbox]:\n{m_lines or 'Inbox clean.'}"

            if any(k in q_lower for k in ("vps", "server", "service", "load", "ram", "cpu", "container", "health")):
                vps_data = await self._query_vps_summary()
                if vps_data:
                    tools_used.append("vps__get_resource_usage")
                    extra_context += f"\n\n[VPS Host Telemetry & Services]:\n{vps_data}"

            if any(k in q_lower for k in ("remember", "who am i", "my name", "my work", "preference")):
                if self.memory.available:
                    facts = self.memory.facts()
                    tools_used.append("memory__search")
                    f_lines = "\n".join(f"• {f.title}: {f.content}" for f in facts[:8])
                    extra_context += f"\n\n[Operator Facts]:\n{f_lines}"

            # Execute completion with LLM provider
            prov = make_provider(model_sec, self.secrets.get)
            system_prompt = (
                f"You are {self.cfg.assistant.name}, an executive digital secretary for Navid.\n"
                f"Operator timezone is {self.cfg.assistant.timezone}. Current time: {now_ts}.\n"
                "Provide direct, concise, well-structured, professional answers.\n"
                "Format with clean Markdown (bullet points, bold key values).\n"
                "If the operator speaks Farsi, reply in natural Farsi."
            )

            # Build history from recent turns
            recent = self.state.recent_turns(cid, 6)
            messages: list[Msg] = []
            for t in recent:
                messages.append(Msg(t["role"], [TextBlock(t["text"])]))

            prompt_body = message
            if extra_context:
                prompt_body += f"\n\n--- Verified Tool Context ---{extra_context}"

            messages.append(Msg("user", [TextBlock(prompt_body)]))

            res = await prov.complete(
                system=system_prompt,
                messages=messages,
                tools=[],
                max_tokens=model_sec.max_tokens,
            )
            assistant_reply = res.text

        except Exception as exc:
            log.exception("Chat generation error")
            assistant_reply = f"I encountered an error executing this request: {exc}"

        reply_ts = now_iso()
        tool_summary = ", ".join(tools_used)
        self.state.add_turn(cid, reply_ts, "assistant", assistant_reply, tool_summary=tool_summary)
        self.state.conversation_touch(cid, reply_ts)

        # Record daily budget
        today = datetime.datetime.now(datetime.UTC).strftime("%Y-%m-%d")
        self.state.budget_add(today, model_calls=1, tool_calls=len(tools_used))
        self.state.audit(
            "model_call",
            conversation=cid,
            origin="web",
            tool=tool_summary,
            outcome="success",
        )

        return JSONResponse(
            {
                "cid": cid,
                "response": assistant_reply,
                "tools": tools_used,
                "ts": reply_ts,
            }
        )

    async def api_chat_history(self, request: Request) -> JSONResponse:
        chat_id = self.cfg.telegram.allowed_user_ids[0] if self.cfg.telegram.allowed_user_ids else 1000
        cid = request.query_params.get("cid", "").strip()
        if not cid:
            conv = self.state.conversation_current(chat_id)
            if not conv:
                row = self.state.x(
                    "SELECT * FROM conversations WHERE chat_id = ? ORDER BY started DESC LIMIT 1",
                    (chat_id,),
                ).fetchone()
                conv = dict(row) if row else None
            cid = str(conv["id"]) if conv else ""

        if not cid:
            return JSONResponse({"cid": "", "turns": []})

        limit = int(request.query_params.get("limit", 100))
        turns = self.state.recent_turns(cid, limit)
        return JSONResponse(
            {
                "cid": cid,
                "turns": [
                    {
                        "id": t["id"],
                        "role": t["role"],
                        "text": t["text"],
                        "ts": t["ts"],
                        "tools": [x.strip() for x in t.get("tool_summary", "").split(",") if x.strip()],
                    }
                    for t in turns
                ],
            }
        )

    async def api_chat_new(self, request: Request) -> JSONResponse:
        chat_id = self.cfg.telegram.allowed_user_ids[0] if self.cfg.telegram.allowed_user_ids else 1000
        cur = self.state.conversation_current(chat_id)
        if cur:
            self.state.conversation_end(cur["id"])
        new_conv = self.state.conversation_new(chat_id, now_iso())
        return JSONResponse({"ok": True, "cid": new_conv["id"]})

    # ---------------------------------------------------------------- Approvals
    async def api_approvals(self, request: Request) -> JSONResponse:
        pending = self.state.approvals_pending()
        history_rows = [
            dict(r)
            for r in self.state.x(
                "SELECT * FROM approvals WHERE status != 'pending' ORDER BY created DESC LIMIT 30"
            ).fetchall()
        ]
        return JSONResponse({"pending": pending, "history": history_rows})

    async def api_approval_action(self, request: Request) -> JSONResponse:
        aid = request.path_params["aid"]
        body = await request.json()
        action = str(body.get("action", "")).strip().lower()

        if action not in ("approved", "rejected"):
            return JSONResponse({"error": "Action must be 'approved' or 'rejected'"}, status_code=400)

        row = self.state.approval_get(aid)
        if not row:
            return JSONResponse({"error": "Approval not found"}, status_code=404)

        self.state.approval_set(aid, status=action)
        self.state.audit(
            "approval_outcome",
            conversation=row.get("conversation"),
            origin="web",
            server=row.get("server"),
            tool=row.get("tool"),
            outcome=action,
        )
        return JSONResponse({"ok": True, "id": aid, "status": action})

    # ---------------------------------------------------------------- Calendar & Gmail
    async def api_calendar(self, request: Request) -> JSONResponse:
        auth = self._get_google_auth()
        if not auth:
            return JSONResponse({"events": [], "note": "Google Calendar not enabled or unauthenticated."})
        try:
            events = await calendar.list_events(auth, timezone=self.cfg.assistant.timezone, max_results=15)
            return JSONResponse({"events": events})
        except Exception as exc:
            return JSONResponse({"events": [], "error": str(exc)}, status_code=500)

    async def api_email(self, request: Request) -> JSONResponse:
        auth = self._get_google_auth()
        if not auth:
            return JSONResponse({"messages": [], "note": "Gmail not enabled or unauthenticated."})
        try:
            messages = await gmail.list_inbox(auth, max_results=10)
            return JSONResponse({"messages": messages})
        except Exception as exc:
            return JSONResponse({"messages": [], "error": str(exc)}, status_code=500)

    # ---------------------------------------------------------------- Knowledge Base
    async def api_kb_documents(self, request: Request) -> JSONResponse:
        docs = self.kb.list_documents()
        return JSONResponse({"documents": docs})

    async def api_kb_search(self, request: Request) -> JSONResponse:
        body = await request.json()
        query = str(body.get("query", "")).strip()
        top_k = int(body.get("top_k", self.cfg.knowledge_base.top_k))
        if not query:
            return JSONResponse({"results": []})
        results = self.kb.search(query, top_k=top_k)
        return JSONResponse({"query": query, "results": results})

    # ---------------------------------------------------------------- Memory
    async def api_memory(self, request: Request) -> JSONResponse:
        if not self.memory.available:
            return JSONResponse({"facts": [], "available": False})
        facts = [
            {
                "id": f.id,
                "subject": f.subject,
                "title": f.title,
                "content": f.content,
                "importance": f.importance,
                "category": f.category,
            }
            for f in self.memory.facts()
        ]
        return JSONResponse({"facts": facts, "available": True})

    # ---------------------------------------------------------------- Schedules
    async def api_schedules(self, request: Request) -> JSONResponse:
        rows = self.state.schedules()
        return JSONResponse({"schedules": rows})

    async def api_schedules_toggle(self, request: Request) -> JSONResponse:
        name = request.path_params["name"]
        for s in self.state.schedules():
            if s["name"] == name:
                new_state = not bool(s["enabled"])
                self.state.schedule_set(name, enabled=new_state)
                return JSONResponse({"ok": True, "name": name, "enabled": new_state})
        return JSONResponse({"error": f"Schedule '{name}' not found"}, status_code=404)

    # ---------------------------------------------------------------- VPS Infrastructure
    async def _query_vps_summary(self) -> str:
        if not self.vps_config_exists:
            return "VPS configuration file not found."
        try:
            from vps_mcp.config.loader import load_config
            from vps_mcp.core import queries
            from vps_mcp.server import build_context

            cfg = load_config(self.vps_config_path)
            ctx = build_context(cfg)
            res = await queries.get_resource_usage(ctx, include_services=False)
            load = res.get("load_avg", [])
            ram_u = res.get("mem_used_bytes", 0) / 1e9
            ram_t = res.get("mem_total_bytes", 0) / 1e9
            return f"Load: {load}, RAM: {ram_u:.1f}/{ram_t:.1f} GB"
        except Exception as exc:
            return f"VPS query error: {exc}"

    async def api_vps(self, request: Request) -> JSONResponse:
        if not self.vps_config_exists:
            return JSONResponse({"error": "VPS configuration not found", "resources": None, "services": []})

        try:
            from vps_mcp.config.loader import load_config
            from vps_mcp.core import queries
            from vps_mcp.server import build_context

            cfg = load_config(self.vps_config_path)
            ctx = build_context(cfg)

            resources_task = asyncio.create_task(queries.get_resource_usage(ctx, include_services=False))
            services_task = asyncio.create_task(queries.list_services(ctx))

            res, svcs_res = await asyncio.gather(resources_task, services_task)
            return JSONResponse({"resources": res, "services": svcs_res.get("services", [])})
        except Exception as exc:
            log.warning("VPS live query error: %s", exc)
            return JSONResponse({"error": str(exc), "resources": None, "services": []}, status_code=500)

    # ---------------------------------------------------------------- Audit
    async def api_audit(self, request: Request) -> JSONResponse:
        rows = [
            dict(r)
            for r in self.state.x(
                "SELECT * FROM audit ORDER BY id DESC LIMIT 50"
            ).fetchall()
        ]
        return JSONResponse({"records": rows})


def create_web_app(cfg: cfgmod.Config | None = None) -> Starlette:
    handler = WebSecretaryApp(cfg)
    routes = [
        Route("/", handler.index, methods=["GET"]),
        Route("/api/status", handler.api_status, methods=["GET"]),
        Route("/api/models", handler.api_models, methods=["GET"]),
        Route("/api/models/switch", handler.api_models_switch, methods=["POST"]),
        Route("/api/chat", handler.api_chat, methods=["POST"]),
        Route("/api/chat/history", handler.api_chat_history, methods=["GET"]),
        Route("/api/chat/new", handler.api_chat_new, methods=["POST"]),
        Route("/api/approvals", handler.api_approvals, methods=["GET"]),
        Route("/api/approvals/{aid}/action", handler.api_approval_action, methods=["POST"]),
        Route("/api/calendar", handler.api_calendar, methods=["GET"]),
        Route("/api/email", handler.api_email, methods=["GET"]),
        Route("/api/knowledge-base", handler.api_kb_documents, methods=["GET"]),
        Route("/api/knowledge-base/search", handler.api_kb_search, methods=["POST"]),
        Route("/api/memory", handler.api_memory, methods=["GET"]),
        Route("/api/schedules", handler.api_schedules, methods=["GET"]),
        Route("/api/schedules/{name}/toggle", handler.api_schedules_toggle, methods=["POST"]),
        Route("/api/vps", handler.api_vps, methods=["GET"]),
        Route("/api/audit", handler.api_audit, methods=["GET"]),
    ]
    return Starlette(routes=routes)


def run_server(
    host: str = "127.0.0.1",
    port: int = 8765,
    open_browser: bool = True,
    cfg: cfgmod.Config | None = None,
) -> None:
    app = create_web_app(cfg)
    url = f"http://{host}:{port}"
    print(f"\n🚀 Digital Secretary UI running at: {url}")
    print("Press Ctrl+C to stop.\n")

    if open_browser:
        asyncio.get_event_loop().call_later(0.5, lambda: webbrowser.open(url))

    uvicorn.run(app, host=host, port=port, log_level="info")


if __name__ == "__main__":
    run_server(open_browser=False)
