"""Lazy tool loading in real turns (feature 008): prefetch, model load, call-load, unload."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from assistant.providers import ToolResult
from assistant.providers.scripted import ScriptedProvider
from tests.assistant_fakes import harness, say, use
from tests.unit.pa.test_flows import approve_flow

OPS: dict[str, Any] = {
    "name": "ops",
    "command": ["unused"],
    "allow": ["*"],
    "policy": {"wipe": "never"},
    "core": ["status"],
    "toolsets": {
        "ops-admin": {
            "tools": ["restart", "deploy", "kick"],
            "description": "restart, deploy and kick services",
            "keywords": ["restart", "deploy", "kick"],
        }
    },
}


async def lazy(tmp: Path, p: ScriptedProvider, **tools: Any) -> Any:
    return await harness(tmp, p, server=[OPS], tools={"lazy": True, **tools})


def names(req: Any) -> list[str]:
    return [t.name for t in req.tools]


def toolset_audit(h: Any) -> list[str]:
    return [
        f"{r['target']} {r['outcome']}"
        for r in h.state.audit_since("2000-01-01")
        if r["kind"] == "toolset"
    ]


def results(req: Any) -> list[ToolResult]:
    return [b for m in req.messages for b in m.content if isinstance(b, ToolResult)]


async def test_everyday_turn_sends_core_and_catalog(tmp_path: Path) -> None:
    p = ScriptedProvider([say("hello"), say("again")])
    h = await lazy(tmp_path, p)
    await h.say("hi there")
    await h.finish()
    sent = names(p.requests[0])
    assert "ops__status" in sent and "tools__load" in sent and "memory__search" in sent
    assert not {"ops__restart", "ops__notes", "files__read"} & set(sent)
    assert "- ops-admin (3): restart, deploy and kick services" in p.requests[0].system
    assert "- ops-more (1): tools: notes" in p.requests[0].system
    assert "wipe" not in p.requests[0].system and "ops__wipe" not in sent
    await h.say("and again")
    await h.finish()
    assert p.requests[0].system == p.requests[1].system  # stable prefix
    calls = [r for r in h.state.audit_since("2000-01-01") if r["kind"] == "model_call"]
    assert " tools=" in calls[-1]["outcome"]


async def test_lazy_off_sends_everything(tmp_path: Path) -> None:
    p = ScriptedProvider([say("ok")])
    h = await harness(tmp_path, p, server=[OPS], tools={"lazy": False})
    await h.say("hi")
    await h.finish()
    assert names(p.requests[0]) == [t.name for t in h.a.agent.all_specs()]
    assert "tools__load" not in names(p.requests[0])
    assert "toolsets" not in p.requests[0].system


async def test_prefetch_loads_matching_toolset_before_first_call(tmp_path: Path) -> None:
    p = ScriptedProvider([say("which one?"), say("done")])
    h = await lazy(tmp_path, p)
    await h.say("please restart the worker")
    await h.finish()
    assert "ops__restart" in names(p.requests[0])
    assert toolset_audit(h) == ["ops-admin loaded: auto (restart)"]
    await h.say("the worker")  # follow-up: still loaded
    await h.finish()
    assert "ops__restart" in names(p.requests[1])


async def test_model_loads_a_toolset_and_uses_it_in_the_same_turn(tmp_path: Path) -> None:
    p = ScriptedProvider(
        [use("tools__load", {"toolsets": ["ops-more"]}), use("ops__notes", uid="t2"), say("done")]
    )
    h = await lazy(tmp_path, p)
    await h.say("anything I should know?")
    await h.finish()
    assert "ops__notes" not in names(p.requests[0])
    assert "ops__notes" in names(p.requests[1])
    loaded = json.loads(results(p.requests[1])[0].content)
    assert loaded == {"loaded": ["ops-more"], "already_loaded": [], "tools": ["ops__notes"]}
    assert "ops-more loaded: model" in toolset_audit(h)
    assert h.tg.texts()[-1] == "done"


async def test_unknown_toolset_is_an_error(tmp_path: Path) -> None:
    p = ScriptedProvider([use("tools__load", {"toolsets": ["gitlab"]}), say("no such tools")])
    h = await lazy(tmp_path, p)
    await h.say("hello")
    await h.finish()
    res = results(p.requests[1])[0]
    assert res.is_error and "unknown toolset 'gitlab'" in res.content and "ops-admin" in res.content
    assert "gitlab refused: unknown toolset 'gitlab'" in toolset_audit(h)


async def test_call_to_unloaded_tool_loads_it_and_still_asks(tmp_path: Path) -> None:
    p = ScriptedProvider([use("ops__restart", {"service": "worker"}), say("Restarted.")])
    h = await lazy(tmp_path, p)
    await h.say("hmm, do it")  # no prefetch match
    assert "ops__restart" not in names(p.requests[0]) if p.requests else True
    await approve_flow(h)
    await h.finish()
    assert h.ops["restarted"] == ["worker"]  # ran only after the approval
    assert "ops-admin loaded: called ops__restart" in toolset_audit(h)


async def test_never_tool_is_never_offered_or_run(tmp_path: Path) -> None:
    p = ScriptedProvider([use("ops__wipe"), say("can't")])
    h = await lazy(tmp_path, p)
    await h.say("wipe everything")
    await h.finish()
    assert all("ops__wipe" not in names(r) for r in p.requests)
    assert h.ops["wiped"] == []
    assert not toolset_audit(h)


async def test_idle_toolsets_unload_and_new_resets(tmp_path: Path) -> None:
    p = ScriptedProvider([say(str(i)) for i in range(10)])
    h = await lazy(tmp_path, p, idle_turns=2)
    await h.say("restart it")
    await h.finish()
    await h.say("/tools")
    assert "▶️ ops-admin (3): auto: restart · this turn" in h.tg.texts()[-1]
    # the last two messages count too (follow-ups), so it stays loaded a little longer
    for msg in ("thanks", "ok", "fine", "cool"):
        await h.say(msg)
        await h.finish()
    assert "ops__restart" not in names(p.requests[-1])
    assert toolset_audit(h)[-1] == "ops-admin unloaded: idle"
    await h.say("restart again")
    await h.finish()
    assert "ops__restart" in names(p.requests[-1])
    await h.say("/new")
    await h.say("hello")
    await h.finish()
    assert "ops__restart" not in names(p.requests[-1])
    await h.say("/tools")
    tools_text = h.tg.texts()[-1]
    assert "▶️" not in tools_text and "ops-admin (3)" in tools_text.split("Not loaded: ")[1]
    assert "core" in tools_text.lower()


async def test_builtin_files_toolset_loads_on_use(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("x")
    p = ScriptedProvider([use("files__list", {"path": str(tmp_path)}), say("one file")])
    h = await lazy(tmp_path, p)
    await h.say("anything there?")
    await h.finish()
    assert "files__list" not in names(p.requests[0]) and "files__list" in names(p.requests[1])
    assert '"a.txt"' in results(p.requests[1])[0].content
