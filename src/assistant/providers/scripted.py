"""ScriptedProvider: replays pre-written replies; records every request (tests)."""

from __future__ import annotations

import copy
from collections.abc import Callable

from assistant.providers import Msg, ProviderError, Reply, Request, ToolSpec

Step = Reply | Exception | Callable[[Request], Reply]


class ScriptedProvider:
    def __init__(self, script: list[Step] | None = None) -> None:
        self.script: list[Step] = list(script or [])
        self.requests: list[Request] = []

    def add(self, *steps: Step) -> None:
        self.script.extend(steps)

    async def complete(
        self, *, system: str, messages: list[Msg], tools: list[ToolSpec], max_tokens: int
    ) -> Reply:
        req = Request(system, copy.deepcopy(messages), list(tools), max_tokens)
        self.requests.append(req)
        if not self.script:
            raise ProviderError("script exhausted")
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        if callable(step) and not isinstance(step, Reply):
            return step(req)
        return step
