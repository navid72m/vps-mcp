# Contract: Toolsets

## `tools__load` (core tool, built-in)

```json
{
  "name": "tools__load",
  "description": "Load one or more toolsets from the catalog so their tools become available from your next step. Use when the operator's request needs tools you don't have yet.",
  "input_schema": {
    "type": "object",
    "properties": {
      "toolsets": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 4}
    },
    "required": ["toolsets"]
  }
}
```

**Result**: a JSON string, not an error:

```json
{"loaded": ["gitlab"], "already_loaded": [], "tools": ["vps__gitlab_tree", "..."]}
```

**Errors** (`is_error` = true):

```json
{"error": "unknown toolset 'x'", "toolsets": ["db", "deploy", "files", "..."]}
{"error": "toolset 'gitlab' is unavailable (server vps is down)"}
```

Each load counts as one tool call against the turn and day budgets.

## Calling a tool that isn't loaded

The model calls `vps__db_table` while `db` isn't loaded. The `db` toolset is loaded, with the audit reason `called vps__db_table`, and the call is handled like any other: policy, approval for an `ask` tool, and argument validation by the tool.

## Catalog (in the system prompt)

```
Tools: you always have the core tools. More tools are grouped into toolsets; load one with
tools__load when you need it (some are loaded automatically from the operator's words):
- db (5): Odoo/Postgres databases: list, overview, modules, tables, compare
- deploy (6): deploy services or GitLab refs, roll back, update and configure services
- files (8): read, write, search, list, send files on this laptop
- gitlab (12): GitLab repos, files, merge requests, issues, pipelines and CI
- jobs (4): run, watch and cancel declared jobs
- ...
```

The catalog is sorted by name and stable for a given configuration and set of servers.

## `/tools` (chat command)

```
🧰 Tools: 11 core + 2 loaded toolsets · ~3.1k tokens (was ~10.5k with everything)
Core: memory__search, memory__get, schedule__add, schedule__list, tools__load, vps__list_services, …
▶️ gitlab (12) — auto: pipeline, gitlab · 1 turn ago
▶️ files (8) — model
   db (5) · deploy (6) · jobs (4) · migration (8) · services (8) · memory-admin (2) · schedules (3)
```

## Config (see data-model.md)

- `[tools] lazy | core | max_auto | idle_turns | threshold`
- `[[server]] core`, `[server.toolsets.<name>] tools | description | keywords`

## `assistant check` additions

```
tools: lazy on · 11 core · 10 toolsets · tool section ~3.0k tokens (all: ~10.5k)
warning: toolset 'deploy' pattern 'deploy_old_*' matches no tool
```
