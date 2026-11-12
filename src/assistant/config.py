"""assistant.toml (data-model §1)."""

from __future__ import annotations

import os
import re
import tomllib
import zoneinfo
from pathlib import Path
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, ValidationError, model_validator

CADENCE_RE = re.compile(
    r"^(?:(daily|weekdays)\s+([01]\d|2[0-3]):([0-5]\d)"
    r"|every\s+(?:([1-9]|1\d|2[0-4])h|([1-9]|[1-5]\d)m)"
    r"|in\s+([1-9]\d*)(s|m|h|d)"
    r"|at\s+([01]\d|2[0-3]):([0-5]\d)"
    r"|(?:at|once)\s+\d{4}-\d{2}-\d{2}(?:\s+|T)\d{2}:\d{2}(?::\d{2})?.*"
    r"|cron\s+\S+\s+\S+\s+\S+\s+\S+\s+\S+)$"
)
SERVER_RE = re.compile(r"^[a-z][a-z0-9_]{0,15}$")
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ALIAS_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,23}$")
Policy = Literal["auto", "ask", "never"]


class ConfigError(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


def _tz(v: str) -> str:
    try:
        zoneinfo.ZoneInfo(v)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError):
        raise ValueError(f"unknown time zone '{v}'") from None
    return v


def _cadence(v: str) -> str:
    if not CADENCE_RE.fullmatch(v):
        raise ValueError("cadence must be 'daily HH:MM', 'weekdays HH:MM' or 'every Nh' (1-24)")
    return v


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class AssistantSection(_Strict):
    name: str = Field(default="Assistant", min_length=1, max_length=40)
    timezone: Annotated[str, AfterValidator(_tz)] = "UTC"
    state_dir: str | None = None
    heartbeat: str = ""

    @model_validator(mode="after")
    def _hb(self) -> AssistantSection:
        if self.heartbeat:
            _cadence(self.heartbeat)
        return self


class TelegramSection(_Strict):
    allowed_user_ids: list[Annotated[int, Field(gt=0)]] = Field(min_length=1, max_length=10)
    offline_max_age_min: int = Field(default=60, ge=1, le=1440)


class SecretsSection(_Strict):
    provider: Literal["keychain", "file"] = "keychain"
    service: str = Field(default="vps-mcp-assistant", pattern=r"^[A-Za-z0-9._-]{1,128}$")
    file: str | None = None


class Prices(_Strict):
    input_per_mtok: float = Field(default=3.0, ge=0)
    output_per_mtok: float = Field(default=15.0, ge=0)


class ModelSection(_Strict):
    alias: str = Field(default="default", pattern=r"^[a-z0-9][a-z0-9-]{0,23}$")
    # "openai" = any OpenAI-compatible chat completions server (LM Studio, Ollama, Gemini, ...)
    provider: Literal["anthropic", "openai"] = "anthropic"
    name: str = Field(default="claude-sonnet-5", min_length=1, max_length=100)
    base_url: str = Field(default="", pattern=r"^(https?://\S+)?$")
    # Keychain/file entry holding the API key; "" = no key (local servers)
    api_key_secret: str | None = Field(default=None, pattern=r"^[a-z0-9_]{0,64}$")
    timeout_s: float = Field(default=300.0, gt=0, le=1800)
    # another profile to try once when this one errors or times out (e.g. a busy free model)
    fallback: str = ""
    vision: bool = True  # can see images (photos are refused politely when false)
    # lazy tool loading for this model; unset = [tools] lazy. Big, fast models can take every
    # tool on every call (false); small or local ones answer faster with fewer (true).
    lazy_tools: bool | None = None
    # "openai" only: sent as reasoning_effort; "none" turns off thinking (faster local models)
    reasoning_effort: Literal["", "none", "minimal", "low", "medium", "high"] = ""
    max_tokens: int = Field(default=2048, ge=64, le=64000)
    prices: Prices = Field(default_factory=Prices)

    @model_validator(mode="after")
    def _provider(self) -> ModelSection:
        if self.provider == "openai" and not self.base_url:
            raise ValueError("model.base_url is required for provider 'openai'")
        if self.api_key_secret is None:
            self.api_key_secret = "anthropic_api_key" if self.provider == "anthropic" else ""
        if self.provider == "anthropic" and not self.api_key_secret:
            raise ValueError("provider 'anthropic' needs model.api_key_secret")
        return self

    @property
    def secret_names(self) -> list[str]:
        return [self.api_key_secret] if self.api_key_secret else []

    @property
    def local(self) -> bool:
        host = re.sub(r"^https?://", "", self.base_url).split("/")[0].split(":")[0]
        return self.provider == "openai" and host in ("localhost", "127.0.0.1", "::1", "[::1]")

    @property
    def where(self) -> str:
        if self.local:
            return "local, nothing leaves the laptop"
        if self.provider == "anthropic":
            return "cloud: Anthropic"
        host = re.sub(r"^https?://", "", self.base_url).split("/")[0]
        return f"cloud: {host}"


class Limits(_Strict):
    turn_tool_calls: int = Field(default=12, gt=0)
    turn_model_calls: int = Field(default=8, gt=0)
    turn_tokens: int = Field(default=60000, gt=0)
    day_tokens: int = Field(default=1_000_000, gt=0)
    day_tool_calls: int = Field(default=300, gt=0)
    day_cost: float = Field(default=5.0, gt=0)
    approval_minutes: int = Field(default=10, ge=1, le=60)
    history_turns: int = Field(default=12, gt=0)
    idle_minutes: int = Field(default=30, gt=0)
    max_proposals: int = Field(default=5, gt=0, le=10)


class MemorySection(_Strict):
    enabled: bool = True
    store: str = ""
    project: str = Field(default="personal", pattern=r"^[a-z0-9][a-z0-9._-]{0,63}$")
    allow_scopes: list[str] = Field(default_factory=list)


TOOLSET_RE = re.compile(r"^[a-z][a-z0-9-]{0,23}$")


class ToolsetSection(_Strict):
    tools: list[str] = Field(min_length=1)  # globs on the server's own tool names
    description: str = Field(default="", max_length=120)
    keywords: list[str] = Field(default_factory=list)


class ToolsSection(_Strict):
    lazy: bool = True  # false: every allowed tool on every call (pre-008 behaviour)
    core: list[str] = Field(default_factory=list)  # extra core tools, model-facing name globs
    max_auto: int = Field(default=2, ge=0, le=5)  # toolsets prefetched per turn
    idle_turns: int = Field(default=6, ge=1)  # unload a used toolset after this many idle turns
    unused_turns: int = Field(default=2, ge=1)  # unload a loaded-but-never-used one sooner
    threshold: float = Field(default=2.0, gt=0)  # prefetch score needed


class WebSection(_Strict):
    enabled: bool = True
    # tavily: free plan, no card (1,000 credits a month); brave: paid plan with a card
    search_provider: Literal["tavily", "brave"] = "tavily"
    search_key_secret: str = Field(
        default="", pattern=r"^[a-z0-9_]{0,64}$"
    )  # "" = <provider>_api_key
    max_results: int = Field(default=5, ge=1, le=10)
    read_chars: int = Field(default=12000, ge=1000, le=50000)
    timeout_s: float = Field(default=20.0, gt=0, le=120)

    @property
    def key_name(self) -> str:
        return self.search_key_secret or f"{self.search_provider}_api_key"


class GoogleSection(_Strict):
    enabled: bool = False
    credentials_path: str = "~/.config/assistant/google_client.json"
    client_id_secret: str = Field(default="google_client_id", pattern=r"^[a-z0-9_]{0,64}$")
    client_secret_secret: str = Field(default="google_client_secret", pattern=r"^[a-z0-9_]{0,64}$")
    token_secret: str = Field(default="google_refresh_token", pattern=r"^[a-z0-9_]{0,64}$")
    max_emails: int = Field(default=10, ge=1, le=50)
    max_events: int = Field(default=20, ge=1, le=100)
    timeout_s: float = Field(default=20.0, gt=0, le=120)


class ServerSection(_Strict):
    name: str
    command: list[str] = Field(default_factory=list)
    allow: list[str] = Field(default_factory=lambda: ["*"])
    policy: dict[str, Policy] = Field(default_factory=dict)
    core: list[str] = Field(default_factory=list)  # always-loaded tools (globs)
    toolsets: dict[str, ToolsetSection] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _rules(self) -> ServerSection:
        if not SERVER_RE.fullmatch(self.name):
            raise ValueError(f"server name '{self.name}' must match {SERVER_RE.pattern}")
        if self.name in (
            "memory",
            "assistant",
            "tools",
            "files",
            "schedule",
            "calendar",
            "email",
        ):
            raise ValueError(f"server name '{self.name}' is reserved for built-in tools")
        for ts in self.toolsets:
            if not TOOLSET_RE.fullmatch(ts):
                raise ValueError(f"toolset name '{ts}' must match {TOOLSET_RE.pattern}")
        return self


class ScheduleSection(_Strict):
    name: str
    cadence: Annotated[str, AfterValidator(_cadence)]
    prompt: str = Field(min_length=1, max_length=2000)
    enabled: bool = True

    @model_validator(mode="after")
    def _slug(self) -> ScheduleSection:
        if not SLUG_RE.fullmatch(self.name):
            raise ValueError(f"schedule name '{self.name}' must be a slug")
        return self


class Config(_Strict):
    assistant: AssistantSection = Field(default_factory=AssistantSection)
    telegram: TelegramSection
    secrets: SecretsSection = Field(default_factory=SecretsSection)
    model: ModelSection = Field(default_factory=ModelSection)  # the default model
    models: dict[str, ModelSection] = Field(default_factory=dict, max_length=10)  # switchable
    limits: Limits = Field(default_factory=Limits)
    tools: ToolsSection = Field(default_factory=ToolsSection)
    web: WebSection = Field(default_factory=WebSection)
    google: GoogleSection = Field(default_factory=GoogleSection)
    memory: MemorySection = Field(default_factory=MemorySection)
    server: list[ServerSection] = Field(default_factory=list, max_length=20)
    schedule: list[ScheduleSection] = Field(default_factory=list, max_length=50)

    @model_validator(mode="after")
    def _unique(self) -> Config:
        for what, names in (
            ("server", [s.name for s in self.server]),
            ("schedule", [s.name for s in self.schedule]),
            (
                "toolset",
                [t for s in self.server for t in s.toolsets]
                + ["files", "schedules", "web", "calendar", "email"],
            ),
        ):
            if len(set(names)) != len(names):
                raise ValueError(f"{what} names must be unique")
        for key, m in self.models.items():
            if not ALIAS_RE.fullmatch(key):
                raise ValueError(f"models.{key}: name must match {ALIAS_RE.pattern}")
            if m.alias == "default":
                m.alias = key
            if m.alias != key:
                raise ValueError(f"models.{key}: alias must be '{key}' (or left out)")
        if self.model.alias in self.models:
            raise ValueError(f"model alias '{self.model.alias}' is also defined under [models]")
        for alias, m in self.profiles.items():
            if m.fallback and (m.fallback == alias or m.fallback not in self.profiles):
                raise ValueError(f"model '{alias}': fallback '{m.fallback}' is not another model")
        return self

    @property
    def profiles(self) -> dict[str, ModelSection]:
        """All switchable models, the default ([model]) first."""
        return {self.model.alias: self.model, **self.models}

    @property
    def state_dir(self) -> Path:
        env = os.environ.get("ASSISTANT_HOME")
        if env:
            return Path(env).expanduser()
        if self.assistant.state_dir:
            return Path(self.assistant.state_dir).expanduser()
        return Path.home() / ".local" / "state" / "assistant"


def default_path() -> Path:
    return Path(
        os.environ.get("ASSISTANT_CONFIG", "~/.config/assistant/assistant.toml")
    ).expanduser()


def load(path: str | Path | None = None) -> Config:
    p = Path(path).expanduser() if path else default_path()
    try:
        raw = tomllib.loads(p.read_text())
    except FileNotFoundError:
        raise ConfigError([f"config file not found: {p}"]) from None
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f"invalid TOML: {exc}"]) from None
    try:
        return Config.model_validate(raw)
    except ValidationError as exc:
        errs = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", ()))
            msg = str(e.get("msg", "invalid")).removeprefix("Value error, ")
            errs.append(f"{loc}: {msg}" if loc else msg)
        raise ConfigError(errs) from None
