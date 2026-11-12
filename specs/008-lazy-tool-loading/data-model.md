# Data Model: Lazy Tool Loading

## Toolset (in memory, rebuilt when servers (re)connect)

| Field | Type | Notes |
|---|---|---|
| name | str | Slug `^[a-z][a-z0-9-]{0,23}$`, unique across all servers. Built-ins: `files`, `schedules`. Fallback groups: `<server>` / `<server>-more`. |
| server | str | `vps`, …, or `builtin` |
| description | str | One line, at most 120 characters. Taken from the configured description, otherwise "tools: a, b, c…" from tool names. |
| tools | list[str] | Model-facing names (`vps__gitlab_tree`) in a fixed order |
| keywords | dict[str, float] | stem → weight (names 2, configured 3, descriptions 0.5). Common description stems dropped (R4). |
| available | bool | The server is up |

**Catalog line** (at most 160 characters): `- gitlab (12): GitLab repos, files, merge requests, issues, pipelines and CI`. Unavailable toolsets get the suffix ` [unavailable]`.

## Core set

An ordered list of model-facing tool names, sent on every call. It's made of:

- the built-in core;
- the global `[tools] core` patterns;
- per-server `core` patterns.

Core tools never belong to a toolset.

## ActiveToolset (SQLite `toolsets_active`)

| Column | Type | Notes |
|---|---|---|
| conversation | TEXT | Conversation id. Part of the primary key with name. |
| name | TEXT | Toolset name |
| loaded_turn | INTEGER | Operator turn number when it was (re)loaded |
| last_used_turn | INTEGER | Turn when one of its tools was last called; 0 = not used yet |
| reason | TEXT | `auto: <matched words>`, `model`, or `called <tool>` |

**Lifecycle**:

- inserted on load;
- `last_used_turn` updated when one of its tools is called;
- deleted on idle unload;
- rows of ended conversations are left in place and are never read again.

## Configuration additions

### `[tools]` (global, all optional)

```toml
[tools]
lazy = true          # false: send every allowed tool on every call (pre-008 behaviour)
core = []            # extra core patterns, as model-facing names: "vps__db_overview"
max_auto = 2         # toolsets prefetched per turn
idle_turns = 6       # unload a used toolset after this many operator turns without use
unused_turns = 2     # unload a loaded-but-never-used toolset after this many
threshold = 2.0      # prefetch score needed
```

### Per `[[server]]` (optional)

```toml
core = ["list_services", "get_service_status"]   # server tool-name globs
[server.toolsets.gitlab]
tools = ["gitlab_*"]
description = "GitLab repos, files, merge requests, issues, pipelines and CI"
keywords = ["gitlab", "repo", "merge request", "pipeline", "ci", "branch", "commit"]
```

### Validation

- Toolset names must be slugs and unique.
- A pattern matching nothing produces a warning in `assistant check`, not an error.
- A tool matched by two toolsets goes to the first one in config order.

## Audit additions

- **New kind `toolset`**:
  - `target` is the toolset name;
  - `outcome` is `loaded: auto (pipeline, gitlab)`, `loaded: model`, `loaded: called vps__db_table`, `unloaded: idle`, or `refused: unknown toolset 'x'`.
- **`model_call` outcome** gains ` tools=<chars>`.

### Per model (optional)

```toml
[model]
lazy_tools = false   # this model gets every tool on every call; unset = [tools] lazy
```
