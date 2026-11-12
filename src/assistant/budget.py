"""Per-turn and per-day limits on tokens, calls and cost (research R15)."""

from __future__ import annotations

import zoneinfo
from dataclasses import dataclass

from assistant.clock import Clock
from assistant.config import Config
from assistant.state import State


class BudgetExceeded(Exception):
    def __init__(self, limit: str, value: float, cap: float) -> None:
        super().__init__(f"budget limit reached: {limit} ({value:g} of {cap:g})")
        self.limit = limit


@dataclass
class TurnCounters:
    model_calls: int = 0
    tool_calls: int = 0
    tokens: int = 0


class Budget:
    def __init__(self, config: Config, state: State, clock: Clock) -> None:
        self.lim = config.limits
        self.prices = config.model.prices
        self.state = state
        self.clock = clock
        self.tz = zoneinfo.ZoneInfo(config.assistant.timezone)
        self.turn = TurnCounters()

    def day(self) -> str:
        return self.clock.wall().astimezone(self.tz).date().isoformat()

    def new_turn(self) -> None:
        self.turn = TurnCounters()

    def cost(self, tin: int, tout: int) -> float:
        return tin / 1e6 * self.prices.input_per_mtok + tout / 1e6 * self.prices.output_per_mtok

    def today(self) -> dict[str, float]:
        return self.state.budget_get(self.day())

    def check_day(self) -> None:
        d = self.today()
        if d["tokens_in"] + d["tokens_out"] >= self.lim.day_tokens:
            raise BudgetExceeded(
                "tokens per day", d["tokens_in"] + d["tokens_out"], self.lim.day_tokens
            )
        if d["cost"] >= self.lim.day_cost:
            raise BudgetExceeded("cost per day", round(d["cost"], 4), self.lim.day_cost)
        if d["tool_calls"] >= self.lim.day_tool_calls:
            raise BudgetExceeded("tool calls per day", d["tool_calls"], self.lim.day_tool_calls)

    def check_model(self) -> None:
        self.check_day()
        if self.turn.model_calls >= self.lim.turn_model_calls:
            raise BudgetExceeded(
                "model calls per turn", self.turn.model_calls, self.lim.turn_model_calls
            )
        if self.turn.tokens >= self.lim.turn_tokens:
            raise BudgetExceeded("tokens per turn", self.turn.tokens, self.lim.turn_tokens)

    def record_model(self, tin: int, tout: int) -> float:
        c = self.cost(tin, tout)
        self.turn.model_calls += 1
        self.turn.tokens += tin + tout
        self.state.budget_add(self.day(), tokens_in=tin, tokens_out=tout, model_calls=1, cost=c)
        return c

    def check_tool(self) -> None:
        if self.turn.tool_calls >= self.lim.turn_tool_calls:
            raise BudgetExceeded(
                "tool calls per turn", self.turn.tool_calls, self.lim.turn_tool_calls
            )
        d = self.today()
        if d["tool_calls"] >= self.lim.day_tool_calls:
            raise BudgetExceeded("tool calls per day", d["tool_calls"], self.lim.day_tool_calls)

    def record_tool(self) -> None:
        self.turn.tool_calls += 1
        self.state.budget_add(self.day(), tool_calls=1)

    def usage_text(self) -> str:
        d = self.today()
        tokens = int(d["tokens_in"] + d["tokens_out"])
        return (
            f"Today ({d['day']}): {int(d['tokens_in'])} in + {int(d['tokens_out'])} out "
            f"tokens ({tokens}/{self.lim.day_tokens}), {int(d['model_calls'])} model calls, "
            f"{int(d['tool_calls'])}/{self.lim.day_tool_calls} tool calls, "
            f"~${d['cost']:.3f} of ${self.lim.day_cost:.2f}."
        )
