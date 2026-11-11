# Research: Declared Jobs

**Feature**: [spec.md](./spec.md) | **Plan**: [plan.md](./plan.md) | **Date**: 2026-11-11

These facts were checked read-only on the operator's VPS. Only generic names are used here, because the repository is public.

- **systemd**: version 249 (Ubuntu 22.04). It supports `systemd-run --setenv`, `--uid`, `RuntimeMaxSec` and `StandardOutput=append:`.
- **Reference job script**: root-owned, mode 0755, inside a 0700 directory. It writes `YYYY-MM-DD HH:MM:SS <EVENT> [details] | free <N>` lines to its progress file, for example:
  - `PIPELINE EXIT 0: …`
  - `RESTORE psql exit 0 , ERROR lines 3`
  - `DEPLOY EXIT 1 (log …)`
  - `COMPARE SUMMARY 6 PASS, 8 FAIL, 1 INFO`
  - `STOP: …`
  - `ALL DONE`
- **The progress file is appended to across runs**, not truncated (27 lines span several runs).
- **The reference job uses `docker exec`** into an unmanaged database container, and `runuser -u postgres psql` against the managed cluster.

---

## J1. Launch: transient systemd unit running the declared script directly

**Decision**: Start the job with a fixed argv. There is no shell and no generated runner.

```text
systemd-run --unit vps-mcp-job-<run_id> --description "vps-mcp job <name>"
  --property=RuntimeMaxSec=<max_runtime_s>        # FR-007 auto-stop
  --property=KillMode=control-group               # cancel kills the whole tree (SC-006)
  --property=StandardOutput=append:<rundir>/console.log
  --property=StandardError=append:<rundir>/console.log
  [--uid=<run_as>] --working-directory=<dirname(script)>
  --setenv=VPSMCP_JOB_RUN=<run_id> [--setenv=<PARAM>=<value> …]   # FR-008, no shell
  /bin/bash -c 'bash "$1"; rc=$?; printf "%s\n" "$rc" > "$2"; exit $rc' vps-mcp-job <script> <rundir>/exit_code
```

- **The wrapper** is a fixed string in the code. The script path and run directory are passed as `$1`/`$2`, never interpolated. It records the exit code, because a successful transient unit is garbage-collected and its status would be lost.
- **Run directory**: `/var/lib/vps-mcp/jobs/<run_id>/`, created with `install -d -m 0700`. It holds the console log and exit code.
- **No `--collect`**, so a failed or timed-out unit keeps its `Result` (`exit-code`, `timeout`, `signal`). Once finalised, the server runs `systemctl reset-failed vps-mcp-job-<id>`.

**Rationale**: Running the real script in place keeps its behaviour identical to a hand run, because it relies on absolute paths and its own `cd`s. `systemd-run` provides the things the spec needs: detachment, a runtime cap, whole-tree kill and a clean environment.

**Alternatives considered**:
- *Copying the script to the run directory and running the copy*: this would close the time-of-check-to-time-of-use window (J3), but breaks scripts that use `$0` or `dirname $0`.
- *The feature-002 generated runner*: unnecessary here, because the job is the operator's script and there are no server-side steps.

## J2. State, exit code and timeout

**Decision**: Derive the state (FR-009) from three sources:
- `systemctl show -p ActiveState,SubState,Result,ExecMainStartTimestamp,ExecMainExitTimestamp --timestamp=utc <unit>`;
- the wrapper's `exit_code` file;
- whether the success marker appeared in the progress lines written since the start.

| Condition | State |
|---|---|
| Unit active | `running` |
| `Result=timeout` | `timed_out` |
| Cancelled by the server (flag set before stop) | `cancelled` |
| `exit_code` = 0 **and** success marker seen | `succeeded` |
| Anything else that ended (non-zero, missing marker, signal, no exit file) | `failed` |

## J3. Script integrity (FR-004, FR-005)

**Decision**:
- **Fingerprint**: `sudo sha256sum -- <script>`.
- **Ownership and mode**: `sudo stat -c '%U %a %n' -- <script> <dir>`.
  - The owner must be root or the declared `run_as` user.
  - No group or other write bit (`mode & 0o022 == 0`) on either the script or its directory.
- **When checked**: at plan time, again immediately before `systemd-run`, and once more when the job ends. A change detected at the end becomes a `script_changed_during_run` warning.

**Rationale**: This closes the realistic risk of someone editing the script between review and run. The remaining sub-second window between the last check and the exec is stated in the plan, and accepted because only root can write the file.

## J4. Progress parsing (FR-010, FR-011)

**Decision**: Read only the lines **written since this job started**:
- At start, record the progress file's byte size with `sudo stat -c %s` (0 if missing).
- When reading, use `sudo tail -c +<offset+1> -- <file>`.
- If the file is now **smaller** than the offset, it was recreated, so read from 0.

**Line grammar** (built-in; patterns declared per job are tried first):

```text
[YYYY-MM-DD HH:MM:SS ]<text>[ | free <N>]
```

Outcome rules, applied in order:
1. The success marker (default `ALL DONE`) means done.
2. A line starting with `STOP` means failed.
3. `EXIT -?\d+`, `exit -?\d+` or `rc=-?\d+`: 0 means ok, anything else means failed.
4. `\bFAILED\b` means failed, and `\bok\b` means ok.
5. A trailing `| free N` is captured as `disk_free`.
6. `\b(\d+) FAIL\b` with N > 0 (e.g. `COMPARE SUMMARY 6 PASS, 8 FAIL`) produces a **warning** finding, "the job reported N failures".

The step name is the text before the first outcome token, e.g. `PIPELINE EXIT 0` → step `PIPELINE`, ok.

Status returns the last 50 lines, plus every recognised outcome line, masked.

## J5. Guard (FR-012, FR-013)

**Decision**:
- **Baseline at start**, built from feature 002's read-only role:
  - `write_counters` for every allowlisted database **not** in `may_modify`, with its session counts;
  - the existence of every allowlisted database (`db_sizes`);
  - the states of every managed service (feature 001) **not** in `services`.
- **Re-read** on every `job_status` call and at the end.

| Change | Finding |
|---|---|
| A write counter rose and the database had 0 other sessions | `undeclared_db_write`, blocking |
| A write counter rose and the database had other sessions | `undeclared_db_write_unattributed`, warning |
| A database present at baseline is gone (not in `may_modify`) | `undeclared_db_dropped`, blocking |
| A service state changed (running ↔ stopped/failed) | `undeclared_service_change`, blocking |

**Scope limit**, stated in the plan: a database inside an *unmanaged* container (like the pipeline's scratch database) and files on disk are not watched.

## J6. Concurrency and hand-started runs (FR-006, edge case)

**Decision**: Starting is refused with `conflict` if any of these is true:
- the local store has a job run in `running`;
- feature 002's store has a rehearsal in `copying`/`running`;
- `systemctl list-units --state=active 'vps-mcp-job-*' 'vps-mcp-rehearsal-*'` shows one;
- `pgrep -f -- <script>` finds a process. This detects a copy started by hand; no sudo is needed, because `/proc` cmdlines are visible.

## J7. Parameters (FR-008, US6)

**Decision**: The declaration is `{name, pattern, default}`.

- **Names**: must match `^[A-Z][A-Z0-9_]{0,31}$`, and must not be any of `PATH`, `HOME`, `SHELL`, `IFS`, `ENV`, `BASH_ENV`, `PS4`, `SHELLOPTS`, `BASHOPTS`, `CDPATH`, `PYTHONPATH`, `PYTHONSTARTUP`, `PERL5LIB`, `NODE_OPTIONS`, or anything starting with `LD_`, `PG`, `SYSTEMD_` or `VPSMCP_`. The config validator rejects them.
- **Values**: must fully match the declared pattern (compiled at config load), be at most 256 characters, and contain no NUL or newline.
- **Defaults**: a literal, or a template with `{date:<strftime>}`, rendered at plan time and shown in the plan.
- **Delivery**: values are passed as `systemd-run --setenv=NAME=value`, a single argv element, so no shell ever parses them.

## J8. Report (FR-014, FR-015)

**Decision**: When a job reaches a final state, the server builds one feature-002 style report (`kind = "job"`) containing:
- **step outcomes**: one entry per recognised outcome line; failed lines become blocking `step_failed` findings;
- **guard findings** (J5);
- **job-reported failures** (J4 rule 6) as a warning;
- **report excerpt**: if a `report_file` is declared, `sudo cat` it, mask it and keep the first 200 lines, flagged as truncated if cut;
- **comparison**: only if the state is `succeeded` and `source` and `result` are declared. `compare_snapshots(source, result)` from feature 002 is reused unchanged. Otherwise the report adds a skipped note giving the reason.

The verdict is `do_not_promote` if the state isn't `succeeded` or any finding is blocking; otherwise feature 002's `verdict()` rules apply.

## J9. Cancel (FR-016)

**Decision**: Cancelling follows these steps:
1. Set `cancel_requested` in the store.
2. Run `sudo systemctl stop vps-mcp-job-<id>`, which kills the whole control group.
3. Verify `ActiveState=inactive` and that `pgrep -f -- <script>` is empty.
4. Report the current state of the declared services and the existence and size of the declared databases.

There is no repair.

**Stated limit**: work running *outside* the job's control group (database server backends executing a query sent by the job, or processes launched with `docker exec` inside a container) is not in the job's process tree. A running query normally ends once its client process is killed. SC-006 is scoped to the job's own processes.

## J10. Sudo additions

Feature 002's `--with-migration` profile already provides `systemd-run`, `sha256sum`, `tail`, `cat` and `systemctl`. This feature adds `/usr/bin/stat` (needed for ownership, mode and progress-file size in root-only directories). It goes in a `--with-jobs` block.
