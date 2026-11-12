"""Switchable model profiles behind one Provider (`/model`, `assistant__switch_model`)."""

from __future__ import annotations

from collections.abc import Callable

from assistant.config import ModelSection
from assistant.providers import Msg, Provider, Reply, ToolSpec

Factory = Callable[[ModelSection], Provider]


class ModelRouter:
    """Delegates to the current profile's provider; providers are built on first use."""

    def __init__(self, profiles: dict[str, ModelSection], factory: Factory, current: str) -> None:
        if current not in profiles:
            raise ValueError(f"unknown model '{current}'")
        self.profiles = profiles
        self._factory = factory
        self._built: dict[str, Provider] = {}
        self.current = current
        self._get(current)  # fail at startup, not on the first message, if the default is broken

    @property
    def profile(self) -> ModelSection:
        return self.profiles[self.current]

    @property
    def max_tokens(self) -> int:
        return self.profile.max_tokens

    @property
    def label(self) -> str:
        return f"{self.current} ({self.profile.name}, {self.profile.where})"

    def provider(self, alias: str) -> Provider:
        return self._get(alias)

    def _get(self, alias: str) -> Provider:
        if alias not in self._built:
            self._built[alias] = self._factory(self.profiles[alias])
        return self._built[alias]

    def switch(self, alias: str) -> ModelSection:
        """Make `alias` current. Raises KeyError (unknown) or the factory's error (e.g. no key)."""
        if alias not in self.profiles:
            raise KeyError(alias)
        self._get(alias)
        self.current = alias
        return self.profile

    async def complete(
        self, *, system: str, messages: list[Msg], tools: list[ToolSpec], max_tokens: int
    ) -> Reply:
        return await self._get(self.current).complete(
            system=system, messages=messages, tools=tools, max_tokens=max_tokens
        )
