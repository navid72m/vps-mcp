"""`[migration]` policy section (contracts/migration-config.md, data-model.md §1)."""

from __future__ import annotations

import posixpath
import re
from typing import Annotated, Literal

from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator

from vps_mcp.models.inputs import AbsPath

DB_NAME_RE = re.compile(r"^[a-z_][a-z0-9_]{0,62}$")
PIPELINE_NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
STEP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
PLACEHOLDER_RE = re.compile(r"\{[^}]*\}")
ALLOWED_PLACEHOLDERS = {"{db}", "{port}"}
COPY_PREFIX = "vmcp_"
DEFAULT_RUN_AS = ("root", "postgres", "odoo", "vpsmcp")


def _db_name(v: str) -> str:
    if not DB_NAME_RE.fullmatch(v):
        raise ValueError(f"database name {v!r} must match ^[a-z_][a-z0-9_]{{0,62}}$")
    if v.startswith(COPY_PREFIX):
        raise ValueError(f"database name {v!r} must not start with {COPY_PREFIX!r} (reserved)")
    return v


def _pipeline_name(v: str) -> str:
    if not PIPELINE_NAME_RE.fullmatch(v):
        raise ValueError(f"pipeline name {v!r} must match ^[a-z0-9-]{{1,40}}$")
    return v


def _step_name(v: str) -> str:
    if not STEP_NAME_RE.fullmatch(v):
        raise ValueError(f"step/check name {v!r} must match ^[a-z0-9][a-z0-9_-]{{0,39}}$")
    return v


def _user(v: str) -> str:
    if not USER_RE.fullmatch(v):
        raise ValueError(f"run_as {v!r} is not a valid user name")
    return v


DbName = Annotated[str, AfterValidator(_db_name)]
PipelineName = Annotated[str, AfterValidator(_pipeline_name)]
StepName = Annotated[str, AfterValidator(_step_name)]
UserName = Annotated[str, AfterValidator(_user)]


class _Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RiskThresholds(_Strict):
    row_drop_warn_pct: float = 5
    row_drop_block_pct: float = 20
    exact_count_trigger_pct: float = Field(default=2, gt=0, le=100)
    # Statistics can be stale after an upgrade, hiding row loss: always count the N largest
    # tables exactly (business-critical data lives there), plus any table flagged above.
    exact_count_top_n: int = Field(default=50, ge=0, le=1000)

    @model_validator(mode="after")
    def _order(self) -> RiskThresholds:
        if not 0 < self.row_drop_warn_pct < self.row_drop_block_pct <= 100:
            raise ValueError("thresholds need 0 < row_drop_warn_pct < row_drop_block_pct <= 100")
        return self


class AnalysisTarget(_Strict):
    name: DbName
    purpose: Literal["dump", "test", "reference"] = "test"


class OdooProfile(_Strict):
    python: AbsPath
    odoo_bin: AbsPath
    config: AbsPath
    pythonpath: list[AbsPath] = Field(default_factory=list)
    addons_paths: list[AbsPath] = Field(default_factory=list, max_length=20)
    run_as: UserName = "odoo"


class StepDefinition(_Strict):
    name: StepName
    kind: Literal["sql", "script", "odoo_upgrade"]
    file: AbsPath | None = None
    modules: Literal["all", "stale"] | None = None
    rounds: int | None = Field(default=None, ge=1, le=5)
    run_as: UserName | None = None
    timeout_s: int | None = Field(default=None, ge=1, le=7 * 24 * 3600)

    @model_validator(mode="after")
    def _shape(self) -> StepDefinition:
        if self.kind in ("sql", "script"):
            if not self.file:
                raise ValueError(f"step '{self.name}': kind={self.kind} requires 'file'")
            if self.modules or self.rounds:
                raise ValueError(f"step '{self.name}': modules/rounds only for odoo_upgrade")
        else:
            if not self.modules:
                raise ValueError(f"step '{self.name}': odoo_upgrade requires 'modules'")
            if self.file:
                raise ValueError(f"step '{self.name}': odoo_upgrade takes no 'file'")
            if self.rounds is not None and self.modules != "stale":
                raise ValueError(f"step '{self.name}': rounds only with modules = 'stale'")
        if self.kind == "sql" and self.run_as not in (None, "postgres"):
            raise ValueError(f"step '{self.name}': sql steps always run as postgres")
        return self


class PipelineDefinition(_Strict):
    name: PipelineName
    steps: list[StepDefinition] = Field(min_length=1, max_length=30)
    progress_file: AbsPath | None = None
    log_globs: list[AbsPath] = Field(default_factory=list, max_length=20)
    progress_patterns: list[str] | None = None

    @model_validator(mode="after")
    def _unique_steps(self) -> PipelineDefinition:
        names = [s.name for s in self.steps]
        if len(names) != len(set(names)):
            raise ValueError(f"pipeline '{self.name}': step names must be unique")
        for pat in self.progress_patterns or []:
            try:
                re.compile(pat)
            except re.error as exc:
                raise ValueError(f"pipeline '{self.name}': bad progress pattern: {exc}") from None
        return self


class CheckDefinition(_Strict):
    name: StepName
    command: list[str] = Field(min_length=1, max_length=40)
    run_as: UserName = "vpsmcp"
    access: Literal["read_only", "may_modify"] = "read_only"
    blocking: bool = False
    timeout_s: int = Field(default=1800, ge=1, le=24 * 3600)

    @model_validator(mode="after")
    def _placeholders(self) -> CheckDefinition:
        for arg in self.command:
            for ph in PLACEHOLDER_RE.findall(arg):
                if ph not in ALLOWED_PLACEHOLDERS:
                    raise ValueError(f"check '{self.name}': placeholder {ph} not allowed")
                if not (arg == ph or arg.endswith("=" + ph)):
                    raise ValueError(
                        f"check '{self.name}': {ph} must be a whole argument or '--opt={ph}'"
                    )
            if "\x00" in arg or "\n" in arg:
                raise ValueError(f"check '{self.name}': arguments must be single-line")
        return self


JOB_NAME_RE = re.compile(r"^[a-z0-9-]{1,40}$")
PARAM_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]{0,31}$")
# Environment names a declared job parameter may never set (research J7).
PARAM_DENY = frozenset(
    {
        "PATH",
        "HOME",
        "SHELL",
        "IFS",
        "ENV",
        "BASH_ENV",
        "PS4",
        "SHELLOPTS",
        "BASHOPTS",
        "CDPATH",
        "PYTHONPATH",
        "PYTHONSTARTUP",
        "PERL5LIB",
        "NODE_OPTIONS",
        "USER",
        "LOGNAME",
    }
)
PARAM_DENY_PREFIXES = ("LD_", "PG", "SYSTEMD_", "VPSMCP_", "BASH_FUNC_")
DATE_TEMPLATE_RE = re.compile(r"\{date:([^}]*)\}")
OUTCOME_GROUPS = {"step", "status"}


def _no_dotdot(v: str) -> str:
    if ".." in v.split("/"):
        raise ValueError(f"path {v!r} must not contain '..'")
    return v


class ParamDecl(_Strict):
    name: str
    pattern: str
    default: str | None = None
    description: str | None = Field(default=None, max_length=200)

    @model_validator(mode="after")
    def _check(self) -> ParamDecl:
        if not PARAM_NAME_RE.fullmatch(self.name):
            raise ValueError(f"parameter {self.name!r} must match ^[A-Z][A-Z0-9_]{{0,31}}$")
        if self.name in PARAM_DENY or self.name.startswith(PARAM_DENY_PREFIXES):
            raise ValueError(f"parameter name {self.name!r} is not allowed")
        try:
            rx = re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"parameter {self.name}: bad pattern: {exc}") from None
        if self.default is not None and not DATE_TEMPLATE_RE.search(self.default):
            if not rx.fullmatch(self.default):
                raise ValueError(f"parameter {self.name}: default does not match its pattern")
        return self


class JobDeclaration(_Strict):
    name: str
    script: AbsPath
    run_as: UserName = "root"
    progress_file: AbsPath
    log_globs: list[AbsPath] = Field(default_factory=list, max_length=20)
    report_file: AbsPath | None = None
    may_modify: list[DbName] = Field(default_factory=list)
    services: list[str] = Field(default_factory=list)
    source: DbName | None = None
    result: DbName | None = None
    params: list[ParamDecl] = Field(default_factory=list, max_length=10)
    min_free_gb: float = Field(default=5, ge=0)
    max_runtime_h: float = Field(default=24, ge=0.1, le=168)
    success_marker: str = Field(default="ALL DONE", min_length=1, max_length=80)
    outcome_patterns: list[str] = Field(default_factory=list, max_length=20)

    @model_validator(mode="after")
    def _check(self) -> JobDeclaration:
        if not JOB_NAME_RE.fullmatch(self.name):
            raise ValueError(f"job name {self.name!r} must match ^[a-z0-9-]{{1,40}}$")
        for p in [
            self.script,
            self.progress_file,
            *(self.log_globs),
            *([self.report_file] if self.report_file else []),
        ]:
            _no_dotdot(p)
        for g in self.log_globs:
            head = g.rsplit("/", 1)[0]
            if any(ch in head for ch in "*?["):
                raise ValueError(f"job {self.name}: wildcards only in the file name ({g})")
        if self.result is not None and self.result not in self.may_modify:
            raise ValueError(f"job {self.name}: result must be listed in may_modify")
        if not self.success_marker.isprintable():
            raise ValueError(f"job {self.name}: success_marker must be printable")
        names = [p.name for p in self.params]
        if len(names) != len(set(names)):
            raise ValueError(f"job {self.name}: parameter names must be unique")
        for pat in self.outcome_patterns:
            try:
                rx = re.compile(pat)
            except re.error as exc:
                raise ValueError(f"job {self.name}: bad outcome pattern: {exc}") from None
            if set(rx.groupindex) - OUTCOME_GROUPS:
                raise ValueError(
                    f"job {self.name}: outcome patterns may only use groups 'step' and 'status'"
                )
        return self


def _inside(path: str, dirs: list[str]) -> bool:
    norm = posixpath.normpath(path)
    return any(norm == d or norm.startswith(d.rstrip("/") + "/") for d in dirs)


class MigrationSettings(_Strict):
    port: int = Field(default=5432, ge=1, le=65535)
    data_dir: AbsPath = "/var/lib/postgresql"
    min_free_gb: float = Field(default=10, ge=0)
    min_free_pct: float = Field(default=10, ge=0, le=90)
    copy_growth_factor: float = Field(default=1.3, ge=1, le=5)
    copy_retention_h: int = Field(default=24, ge=1, le=24 * 30)
    analysis_timeout_s: int = Field(default=300, ge=5, le=3600)
    step_timeout_s: int = Field(default=21600, ge=60, le=7 * 24 * 3600)
    approved_dirs: list[AbsPath] = Field(default_factory=list, max_length=20)
    allowed_run_as: list[UserName] = Field(default_factory=list)
    thresholds: RiskThresholds = Field(default_factory=RiskThresholds)
    target: list[AnalysisTarget] = Field(default_factory=list, max_length=50)
    odoo: OdooProfile | None = None
    pipeline: list[PipelineDefinition] = Field(default_factory=list, max_length=10)
    check: list[CheckDefinition] = Field(default_factory=list, max_length=20)
    job: list[JobDeclaration] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def _cross(self) -> MigrationSettings:
        names = [t.name for t in self.target]
        if len(names) != len(set(names)):
            raise ValueError("migration target names must be unique")
        for kind, items in (("pipeline", self.pipeline), ("check", self.check)):
            n = [i.name for i in items]
            if len(n) != len(set(n)):
                raise ValueError(f"migration {kind} names must be unique")
        users = set(DEFAULT_RUN_AS) | set(self.allowed_run_as)
        for p in self.pipeline:
            for s in p.steps:
                if s.file and not _inside(s.file, self.approved_dirs):
                    raise ValueError(
                        f"pipeline '{p.name}' step '{s.name}': {s.file} is outside approved_dirs"
                    )
                if s.kind == "odoo_upgrade" and self.odoo is None:
                    raise ValueError(
                        f"pipeline '{p.name}' step '{s.name}': odoo_upgrade needs [migration.odoo]"
                    )
                if s.run_as and s.run_as not in users:
                    raise ValueError(f"step '{s.name}': run_as '{s.run_as}' not allowed")
        self._check_jobs()
        for c in self.check:
            if c.run_as not in users:
                raise ValueError(f"check '{c.name}': run_as '{c.run_as}' not allowed")
            head = c.command[0]
            interpreters = {self.odoo.python} if self.odoo else set()
            if head not in interpreters and not _inside(head, self.approved_dirs):
                raise ValueError(
                    f"check '{c.name}': {head} must be [migration.odoo].python or inside "
                    "approved_dirs"
                )
            for arg in c.command[1:]:
                if arg.startswith("/") and not _inside(arg, self.approved_dirs):
                    raise ValueError(f"check '{c.name}': path {arg} is outside approved_dirs")
                if ".." in arg.split("/"):
                    raise ValueError(f"check '{c.name}': '..' is not allowed in arguments")
        return self

    def _check_jobs(self) -> None:
        targets = set(self.target_names())
        users = set(DEFAULT_RUN_AS) | set(self.allowed_run_as)
        names = [j.name for j in self.job]
        if len(names) != len(set(names)):
            raise ValueError("job names must be unique")
        for j in self.job:
            for db in [
                *j.may_modify,
                *([j.source] if j.source else []),
                *([j.result] if j.result else []),
            ]:
                if db not in targets:
                    raise ValueError(f"job {j.name}: database '{db}' is not a [[migration.target]]")
            if j.run_as not in users:
                raise ValueError(f"job {j.name}: run_as '{j.run_as}' not allowed")

    def get_job(self, name: str) -> JobDeclaration | None:
        return next((j for j in self.job if j.name == name), None)

    def target_names(self) -> list[str]:
        return [t.name for t in self.target]

    def get_pipeline(self, name: str) -> PipelineDefinition | None:
        return next((p for p in self.pipeline if p.name == name), None)

    def get_check(self, name: str) -> CheckDefinition | None:
        return next((c for c in self.check if c.name == name), None)
