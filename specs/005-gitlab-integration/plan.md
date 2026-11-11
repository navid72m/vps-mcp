# Implementation Plan: GitLab Integration

**Branch**: `005-gitlab-integration` | **Date**: 2026-11-11 | **Spec**: [spec.md](./spec.md)

**Input**: Feature specification from `/specs/005-gitlab-integration/spec.md`

## Summary

This adds 15 tools that give assistants access to the operator's GitLab, using the operator's own token from the macOS Keychain. Access is limited to allowlisted projects, each with its own permissions.

**Reading and CI**
- Reads (code, history, issues, MRs, pipelines, job logs) are compact, size-capped and masked.
- Text written by other people is returned as labelled external content.

**Writes**
- Issue and MR writes and CI actions show a preview, need confirmation and are audited.
- There is no tool that merges.

**Deploys**
- The **laptop** downloads the exact commit's archive and sanitises it into a clean tar.
- The tar is streamed over the existing SSH connection into a new release directory on the VPS.
- The destination symlink is switched atomically.
- The declared services are restarted and health-checked. If they fail, the server rolls back automatically.
- The token never touches the VPS.

**Memory**: deployed versions and GitLab activity feed feature 004's memory.

Decisions G1–G14 are in [research.md](./research.md).

## Technical Context

**Language/Version**: Python 3.12+, in the same package.

**Primary Dependencies**
- `httpx2`: already installed as a dependency of the MCP SDK; now declared explicitly.
- macOS `security` CLI, used for the Keychain.
- On the VPS: `tar`, coreutils and `curl` (only when `health_url` is set).

**Storage**
- **State DB**: SQLite schema v5 adds `gitlab_deploys`.
- **VPS**: releases live under `/var/lib/vps-mcp/deploys/<target>/releases/`, and each destination is a symlink to the active release.

**Testing**: pytest with these pieces:
- `tests/fakegitlab.py`: an in-process fake GitLab built on an `httpx2.MockTransport`. It holds projects, refs, trees, files, commits, compare, issues, MRs, notes, pipelines, jobs and logs, and archive tarballs built on the fly.
- `FakeVps` additions: binary stdin, `tar -x` into an in-memory file map, `ln`/`mv -T`/`readlink`, release markers and `sha256sum -c`.
- The token-leak test.
- A negative suite of at least 30 cases.

**Target Platform**: the laptop (macOS) for GitLab and Keychain access; the same VPS as before for deploys.

**Project Type**: an extension of the single package, as a new subpackage `vps_mcp.gitlab`.

**Performance Goals**
- Reads take ≤ 3 s on a normal network.
- A deploy of a 50 MB archive finishes in under 2 minutes, excluding service start (SC-001, SC-004).

**Constraints**
- Allowlist and permissions are checked before any network call.
- Every read is size-capped.
- The token only ever appears in one HTTP header.
- Deploys are file-only: no build steps.

**Scale/Scope**: 1–10 projects, 1–5 deploy targets, archives up to 200 MB.

## Constitution Check

*GATE: Must pass before Phase 0 research. Re-check after Phase 1 design.*

The constitution is still the unfilled template, so there are no ratified gates. **PASS**.

The engineering defaults from features 001–004 apply:

- **Least privilege**:
  - one alias-based allowlist;
  - permissions per project;
  - no merge tool;
  - the token stays on the laptop.
- **Testability**: GitLab and the VPS are both faked, and the sanitiser and plan/hash logic are pure functions.
- **Simplicity**: no git on the VPS and no agent on the VPS.
- **Observability**: every write and deploy is audited and recorded in memory.

**Post-design re-check**: PASS. These residual risks are documented:

1. The deploy sudo rights (`tar`, `rm`, `chown`, …) are root-equivalent. As before, safety relies on the server only issuing validated argv.
2. The operator's token may have broader GitLab rights than the declared permissions. The allowlist limits what agents can do through vps-mcp, but not the token itself. The docs recommend project or group tokens with the minimum role.

## Project Structure

### Documentation (this feature)

```text
specs/005-gitlab-integration/
├── plan.md  research.md  data-model.md  quickstart.md
├── contracts/ (gitlab-tools.md, gitlab-config.md)
├── checklists/requirements.md
└── tasks.md                  # /speckit-tasks
```

### Source Code (repository root)

```text
src/vps_mcp/
├── gitlab/
│   ├── __init__.py
│   ├── config.py           # GitLabSettings, ProjectDecl, DeployTarget + validation (data-model §1)
│   ├── credentials.py      # Keychain lookup via `security`, lazy, registers token for redaction (G2)
│   ├── client.py           # GitLabClient: httpx2 AsyncClient, error mapping, caps, pagination (G1, G5)
│   ├── access.py           # alias resolution + permission checks (G3, G4)
│   ├── render.py           # compact shapes, external_content envelope, masking (G5, G6)
│   ├── read.py             # refs, tree, file, commits, compare
│   ├── items.py            # issues/MRs list/read + gitlab_write (G7, G8)
│   ├── ci.py               # pipelines, jobs, job logs, gitlab_ci
│   ├── archive.py          # download + sanitize tar + manifest (G9), pure-ish
│   ├── deploy.py           # plan/confirm/switch/health/rollback/prune (G10–G13)
│   ├── store.py            # gitlab_deploys accessors
│   └── tools.py            # 15 MCP tools
├── remote/executor.py      # + stdin_bytes (binary stdin)
├── store/db.py             # schema v5
├── store/audit.py          # + gitlab_* actions, rollback_deploy
├── memory/derived.py       # + deployed versions, GitLab activity (G14)
├── memory/service.py       # + refresh set entries
├── config/policy.py        # AppConfig.gitlab + cross-checks (services, destinations)
├── server.py               # register gitlab tools; token into secret set
└── cli.py                  # gitlab-check; bootstrap --with-gitlab

tests/
├── fakegitlab.py
├── fakes.py                # + binary stdin, tar/ln/mv/readlink/sha256sum -c emulation
├── unit/gitlab/            # config, credentials, client errors, read, items, ci, archive, deploy, leak
└── negative/test_gitlab_disallowed.py
```

**Structure Decision**:

- GitLab is its own subpackage. It reuses feature 001's backends (restart and health), the executor and the guard (read-only and audit), and feature 004's masking.
- Tools are registered only when `[gitlab]` exists, so existing configs are unaffected.

## Implementation Phasing (for /speckit-tasks)

1. **Foundation**:
   - config models and validation;
   - Keychain credentials and redaction;
   - client with error mapping;
   - access checks;
   - render and masking;
   - schema v5;
   - audit actions;
   - the fake GitLab;
   - executor `stdin_bytes`.
2. **US5 + US1 (P1)**: the token-missing path, `gitlab-check`, the read tools and the token-leak test harness.
3. **US2 (P1)**: archive sanitiser, FakeVps tar and symlink emulation, deploy plan/confirm/switch/health/rollback/prune, `deploy_targets`, `rollback_deploy`, sudoers `--with-gitlab`.
4. **US3 (P2)**: listing and reading items, and `gitlab_write`, including the close rules.
5. **US4 (P2)**: pipelines, job logs and `gitlab_ci`, plus the pipeline gate in the deploy plan.
6. **Memory integration**: derived sections and the refresh set.
7. **Polish**:
   - negative suite of at least 30 cases;
   - full leak sweep;
   - README section;
   - example config;
   - live validation with the operator's GitLab (read-only first).

## Complexity Tracking

There are no constitution violations. The two residual risks above are documented and repeated in the README.
