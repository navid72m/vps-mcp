"""Render the detached rehearsal runner (research M5, M6).

The runner is a set of bash files generated from fixed templates; every interpolated value
is shell-quoted. It runs as root in a transient systemd unit, so it keeps going when the
MCP client disconnects. It appends one event per line to events.log, which the server
parses for status. The sha256 over all rendered files is part of the confirmed plan.
"""

from __future__ import annotations

import hashlib
import posixpath
import re
import shlex
from typing import Any

from vps_mcp.migration.config import MigrationSettings, PipelineDefinition, StepDefinition
from vps_mcp.migration.copies import copy_commands

REHEARSAL_ROOT = "/var/lib/vps-mcp/rehearsals"
_EVENT_RE = re.compile(r"^(?P<ts>\d+) (?P<kind>[A-Z]+)(?: (?P<rest>.*))?$")
TERMINAL = (
    "succeeded",
    "failed",
    "timed_out",
    "aborted_disk",
    "foreign_write",
    "copy_failed",
    "cancelled",
)

q = shlex.quote


def rehearsal_dir(rehearsal_id: str) -> str:
    return f"{REHEARSAL_ROOT}/{rehearsal_id}"


def _odoo_cmd(settings: MigrationSettings, copy: str, modules_expr: str, logfile: str) -> str:
    odoo = settings.odoo
    assert odoo is not None
    env = ["env"]
    if odoo.pythonpath:
        env.append(q("PYTHONPATH=" + ":".join(odoo.pythonpath)))
    return " ".join(
        [
            f"runuser -u {q(odoo.run_as)} --",
            *env,
            q(odoo.python),
            q(odoo.odoo_bin),
            "-c",
            q(odoo.config),
            "-d",
            q(copy),
            q(f"--db-filter=^{copy}$"),
            q(f"--db_port={settings.port}"),
            "--max-cron-threads",
            "0",
            "-u",
            modules_expr,
            "--no-http",
            "--workers",
            "0",
            "--stop-after-init",
            "--log-level=warn",
            q(f"--logfile={logfile}"),
        ]
    )


def step_script(
    n: int,
    step: StepDefinition,
    settings: MigrationSettings,
    copy: str,
    rdir: str,
    old_major: str | None,
) -> str:
    port = str(settings.port)
    out_dir = f"{rdir}/out-{n}"
    lines = ["#!/bin/bash", f"# vps-mcp rehearsal step {n}: {step.name} ({step.kind})", "set -u"]
    if step.kind == "sql":
        assert step.file
        lines.append(
            f"exec runuser -u postgres -- psql -X -p {port} -d {q(copy)} "
            f"-v ON_ERROR_STOP=1 -f {q(step.file)}"
        )
    elif step.kind == "script":
        assert step.file
        run_as = step.run_as or "root"
        psql = f"runuser -u postgres -- psql -X -p {port} -d {copy}"
        repo = settings.odoo.addons_paths[0] if settings.odoo and settings.odoo.addons_paths else ""
        env = [
            f"DB={q(copy)}",
            f"PGDATABASE={q(copy)}",
            f"PGPORT={port}",
            f"PSQL={q(psql)}",
            f"REPO={q(repo)}",
            f"OUT={q(out_dir + '/out.tsv')}",
        ]
        prefix = "" if run_as == "root" else f"runuser -u {q(run_as)} -- "
        lines.append(f"cd {q(out_dir)}")
        lines.append(f"exec {prefix}env {' '.join(env)} bash {q(step.file)}")
    else:
        assert settings.odoo is not None
        lines.append(f"cd {q(posixpath.dirname(settings.odoo.odoo_bin))}")
        logfile = f"{out_dir}/odoo.log"
        if step.modules == "all":
            lines.append("exec " + _odoo_cmd(settings, copy, "all", logfile))
        else:
            assert old_major and old_major.isdigit()
            addons = " ".join(q(p) for p in (settings.odoo.addons_paths or []))
            stale_sql = (
                "SELECT coalesce(string_agg(name, ',' ORDER BY name), '') "  # noqa: S608 - major is a validated digit string
                "FROM ir_module_module WHERE state IN ('installed','to upgrade') "
                f"AND split_part(latest_version, '.', 1) = '{old_major}'"
            )
            lines += [
                f"for round in $(seq 1 {step.rounds or 1}); do",
                f"  mods=$(runuser -u postgres -- psql -X -p {port} -d {q(copy)} -At "
                f"-c {q(stale_sql)}) || exit 1",
                '  [ -z "$mods" ] && { echo "no stale modules left"; exit 0; }',
                "  keep=()",
                '  IFS=, read -ra arr <<< "$mods"',
                '  for m in "${arr[@]}"; do',
                f"    for d in {addons}; do",
                '      [ -f "$d/$m/__manifest__.py" ] && { keep+=("$m"); break; }',
                "    done",
                "  done",
                '  [ ${#keep[@]} -eq 0 ] && { echo "stale modules have no code: $mods"; exit 0; }',
                '  echo "round $round: ${keep[*]}"',
                "  "
                + _odoo_cmd(settings, copy, '"$(IFS=,; echo "${keep[*]}")"', logfile)
                + " || exit 1",
                "done",
                "exit 0",
            ]
    return "\n".join(lines) + "\n"


def run_script(
    *,
    rehearsal_id: str,
    pipeline: PipelineDefinition,
    settings: MigrationSettings,
    source: str,
    copy: str,
    owner: str,
    method: str,
    marker: str,
    file_shas: dict[str, str],
    watch: list[str],
    min_free: int,
) -> str:
    rdir = rehearsal_dir(rehearsal_id)
    port = settings.port
    watch_sql = ",".join(f"'{w}'" for w in sorted(set(watch)))  # validated identifiers
    counters_sql = (
        "SELECT d.datname, d.tup_inserted + d.tup_updated + d.tup_deleted, "  # noqa: S608 - validated identifiers
        "(SELECT count(*) FROM pg_stat_activity a WHERE a.datname = d.datname "
        "AND a.backend_type = 'client backend') FROM pg_stat_database d "
        f"WHERE d.datname IN ({watch_sql or chr(39) * 2}) ORDER BY 1"
    )
    out = [
        "#!/bin/bash",
        f"# generated by vps-mcp: rehearsal {rehearsal_id} of pipeline {pipeline.name}",
        "set -u",
        "umask 077",
        f"DIR={q(rdir)}",
        'EV="$DIR/events.log"',
        f"PORT={port}",
        f"COPY={q(copy)}",
        f"DATA_DIR={q(settings.data_dir)}",
        f"MIN_FREE={int(min_free)}",
        'ev() { printf \'%s %s\\n\' "$(date +%s)" "$*" >> "$EV"; }',
        f"counters() {{ runuser -u postgres -- psql -X -p \"$PORT\" -d postgres -At -F ' ' "
        f"-c {q(counters_sql)}; }}",
        "free_ok() { local f; f=$(df -B1 --output=avail \"$DATA_DIR\" | tail -1 | tr -d ' '); "
        '[ "$f" -ge "$MIN_FREE" ]; }',
        "check_foreign() {",
        "  local n=$1 rc=0 db w c bw",
        "  while read -r db w c; do",
        '    bw=$(awk -v d="$db" \'$1==d{print $2}\' "$DIR/w$n.before")',
        '    [ -n "$bw" ] || continue',
        '    if [ "$w" -gt "$bw" ]; then',
        '      if [ "$c" -eq 0 ]; then ev "FOREIGN $n $db $((w-bw)) attributed"; rc=1;',
        '      else ev "FOREIGN $n $db $((w-bw)) unattributed"; fi',
        "    fi",
        '  done < "$DIR/w$n.after"',
        "  return $rc",
        "}",
        "LAST=ok",
        "run_step() {",
        "  local n=$1 file=$2 sha=$3 tmo=$4 user=$5 cur rc st",
        '  if [ -n "$file" ]; then',
        "    cur=$(sha256sum -- \"$file\" 2>/dev/null | cut -d' ' -f1)",
        '    if [ "$cur" != "$sha" ]; then ev "STEP $n failed 1 fingerprint_changed"; '
        "LAST=failed; return 1; fi",
        "  fi",
        '  free_ok || { ev "ABORT disk"; return 2; }',
        '  install -d -m 0700 -o "$user" "$DIR/out-$n"',
        '  counters > "$DIR/w$n.before" 2>/dev/null',
        '  ev "STEP $n running"',
        '  timeout --kill-after=60 "$tmo" /bin/bash "$DIR/step-$n.sh" '
        '> "$DIR/step-$n.log" 2>&1; rc=$?',
        '  sleep 2; counters > "$DIR/w$n.after" 2>/dev/null',
        "  if [ $rc -eq 124 ] || [ $rc -eq 137 ]; then st=timeout; "
        "elif [ $rc -eq 0 ]; then st=ok; else st=failed; fi",
        '  ev "STEP $n $st $rc"',
        "  LAST=$st",
        '  check_foreign "$n" || return 3',
        '  [ "$st" = ok ] || return 1',
        "}",
        'finish() { ev "RUN $1"; exit 0; }',
        "after_step() {",
        "  case $1 in",
        "    0) return 0 ;;",
        "    2) finish aborted_disk ;;",
        "    3) finish foreign_write ;;",
        '    *) if [ "$LAST" = timeout ]; then finish timed_out; else finish failed; fi ;;',
        "  esac",
        "}",
        "COPY_DONE=0",
        "on_term() {",
        "  # a half-created copy has no marker yet and could never be dropped later: drop it now",
        '  if [ "$COPY_DONE" != 1 ]; then',
        '    runuser -u postgres -- dropdb -p "$PORT" --if-exists "$COPY" >> "$DIR/copy.log" 2>&1',
        "  fi",
        '  ev "RUN cancelled"; exit 0',
        "}",
        "trap on_term TERM",
        'ev "RUN started"',
        "free_ok || finish aborted_disk",
        'ev "COPY started"',
        f"if ( {copy_commands(method, source, copy, owner, port, marker)}",
        ') > "$DIR/copy.log" 2>&1; then',
        "  COPY_DONE=1",
        '  ev "COPY ok"',
        "else",
        '  rc=$?; ev "COPY failed $rc"',
        '  runuser -u postgres -- dropdb -p "$PORT" --if-exists "$COPY" >> "$DIR/copy.log" 2>&1',
        "  finish copy_failed",
        "fi",
    ]
    default_timeout = settings.step_timeout_s
    for n, step in enumerate(pipeline.steps, start=1):
        user = "root"
        if step.kind == "sql":
            user = "postgres"
        elif step.kind == "script":
            user = step.run_as or "root"
        elif settings.odoo:
            user = settings.odoo.run_as
        sha = file_shas.get(step.file or "", "")
        out.append(
            f"run_step {n} {q(step.file or '')} {q(sha)} "
            f"{int(step.timeout_s or default_timeout)} {q(user)}; after_step $?"
        )
    out.append("finish succeeded")
    return "\n".join(out) + "\n"


def render(
    *,
    rehearsal_id: str,
    pipeline: PipelineDefinition,
    settings: MigrationSettings,
    source: str,
    copy: str,
    owner: str,
    method: str,
    marker: str,
    file_shas: dict[str, str],
    watch: list[str],
    min_free: int,
    old_major: str | None,
) -> tuple[dict[str, str], str]:
    """Return ({filename: content}, sha256 over all files)."""
    rdir = rehearsal_dir(rehearsal_id)
    files = {
        "run.sh": run_script(
            rehearsal_id=rehearsal_id,
            pipeline=pipeline,
            settings=settings,
            source=source,
            copy=copy,
            owner=owner,
            method=method,
            marker=marker,
            file_shas=file_shas,
            watch=watch,
            min_free=min_free,
        )
    }
    for n, step in enumerate(pipeline.steps, start=1):
        files[f"step-{n}.sh"] = step_script(n, step, settings, copy, rdir, old_major)
    h = hashlib.sha256()
    for name in sorted(files):
        h.update(name.encode() + b"\0" + files[name].encode() + b"\0")
    return files, h.hexdigest()


def parse_events(text: str, n_steps: int) -> dict[str, Any]:
    """Fold events.log into {status, copy, steps[], foreign[], aborted_disk}."""
    steps: list[dict[str, Any]] = [{"n": i, "status": "not_run"} for i in range(1, n_steps + 1)]
    result: dict[str, Any] = {
        "status": "copying",
        "copy": "pending",
        "steps": steps,
        "foreign": [],
        "final": None,
    }
    for line in text.splitlines():
        m = _EVENT_RE.match(line.strip())
        if not m:
            continue
        kind, rest, ts = m["kind"], (m["rest"] or "").split(), int(m["ts"])
        if kind == "COPY" and rest:
            result["copy"] = rest[0]
            if rest[0] == "ok":
                result["status"] = "running"
        elif kind == "STEP" and len(rest) >= 2 and rest[0].isdigit():
            i = int(rest[0]) - 1
            if 0 <= i < n_steps:
                st = steps[i]
                st["status"] = rest[1]
                if rest[1] == "running":
                    st["started"] = ts
                else:
                    st["ended"] = ts
                    if len(rest) > 2 and rest[2].lstrip("-").isdigit():
                        st["exit_code"] = int(rest[2])
                    if len(rest) > 3:
                        st["reason"] = rest[3]
        elif kind == "FOREIGN" and len(rest) >= 4:
            result["foreign"].append(
                {
                    "step": int(rest[0]),
                    "database": rest[1],
                    "writes": int(rest[2]),
                    "attributed": rest[3] == "attributed",
                }
            )
        elif kind == "RUN" and rest and rest[0] in TERMINAL:
            result["final"] = rest[0]
            result["status"] = rest[0]
    if result["final"] == "cancelled":
        for st in steps:
            if st["status"] == "running":
                st["status"] = "cancelled"
    return result
