# Quickstart / Validation: Agent Memory Service

## 1. Automated checks

```bash
uv run pytest tests/unit/agent_memory tests/negative/test_agent_memory_disallowed.py -q
uv run ruff check && uv run mypy
uv run agent-memory bench --fixtures builtin
```

The tests and checks must pass, and the benchmark scorecard (`ranked` configuration) must show:

| Metric | Target |
|---|---|
| recall@8 | ≥ 0.85 |
| precision@8 | ≥ 0.60 |
| stale-hit rate | 0 |
| contradiction-flag rate | 1.0 |
| average tokens per result | ≤ 1,000 |
| pinned core | ≤ ~500 tokens |

The `naive` and `none` configurations are printed alongside for comparison.

## 2. Write policy (SC-004)

`tests/unit/agent_memory/test_write_policy.py` runs 30 seeded candidates. Every one must get the expected outcome:

- created;
- confirmed (duplicate);
- conflict (flagged);
- refused (secret);
- refused (untrusted instruction);
- downgraded to unverified (untrusted source);
- marked inferred.

## 3. Standalone server in two clients (SC-005)

```bash
# client A (e.g. Claude Code), project "shop"
claude mcp add --scope user memory -- "$(which uv)" run --directory "$PWD" agent-memory serve --project shop --client claude-code
# client B (e.g. OpenCode): same command with --client opencode
```

1. In A, say: "Remember as a procedure: always back up before migrating." The memory is created.
2. In B, start a new session. The pinned core lists it if it's pinned. "What do we know about migrations?" returns it.
3. Start B with `--project other` and repeat the question. The memory is not returned.

## 4. Lifecycle (US3)

1. Save the semantic fact "postgres.version = 16".
2. Supersede it with "17", giving a reason.
3. Search "postgres version". Only 17 comes back. `memory_get` shows both versions and the reason.
4. Save a fact with `expires=1d`. After that time passes (tests use a fake clock), it is gone from search, and `doctor` lists it as expired.

## 5. Migration of feature 004 (SC-006)

```bash
uv run agent-memory migrate-004 ~/.config/vps-mcp/myvps.memory --project vps-myvps --environment myvps          # dry run
uv run agent-memory migrate-004 ~/.config/vps-mcp/myvps.memory --project vps-myvps --environment myvps --apply
```

- The dry run lists `note → type/scope/subject` for every note.
- `--apply` writes them, and the source folder is unchanged.
- In vps-mcp, `[memory]` now points at the store. The connect instructions show the pinned core, not the old briefing.

## 6. vps-mcp generated facts (FR-023, SC-008)

1. In vps-mcp, call `list_services`. Memory gains or updates `service.<name>.state` facts in `environment:<vps>`, marked live-state. Calling it again updates them in place, without duplicates.
2. Make the store unreadable and start vps-mcp. All other tools still work, and the instructions say memory is unavailable.
