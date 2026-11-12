# Quickstart: Lazy Tool Loading

## 1. Automated checks

```bash
uv run pytest -q tests/unit/pa/test_toolsets.py tests/unit/pa/test_lazy_flow.py \
  tests/unit/pa/test_prefetch_quality.py
uv run pytest -q          # everything, with lazy on (SC-004)
```

| Expected | Tests |
|---|---|
| Grouping, catalog, scoring and persistence pass | `test_toolsets.py` |
| Prefetch, model-requested load, unloaded-call retry, idle unload, `/new` reset, `lazy=false` parity | `test_lazy_flow.py` |
| ≥ 80% prefetch hits on the 20 fixed questions (SC-002) | `test_prefetch_quality.py` |

## 2. Size check (SC-001)

```bash
uv run assistant check
```

The `tools:` line shows the tool-section size with lazy loading, next to the size with everything loaded. The lazy size must be ≤ 35% of the full size.

## 3. Live check on the phone

Send these with the bot running, then read `uv run assistant audit --since 10m`:

| Send | Expected |
|---|---|
| "Is the server OK?" | Answered with core tools; no `toolset` audit lines; the `model_call` record shows `tools=` about 12k characters or less |
| "Why did the last pipeline fail?" | A `toolset gitlab loaded: auto (pipeline)` line before the first model call; a GitLab tool is used |
| `/tools` | `gitlab` shown as loaded |
| Six unrelated messages ("thanks", "what time is it", …), then `/tools` | `gitlab` unloaded (`unloaded: idle` in the audit) |
| "Look at the er rehearsal from yesterday" | `migration` loaded automatically, or by the model, within the same turn |
| `/new`, then `/tools` | Only the core is loaded |
