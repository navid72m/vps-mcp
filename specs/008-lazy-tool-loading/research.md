# Research: Lazy Tool Loading

## Baseline

Measured on 2026-11-12 with the operator's configuration:

| Measure | Value |
|---|---|
| Tools | 64 |
| Size of tool definitions | 41,994 characters, about 10,500 tokens, on every call |
| Largest group | vps gitlab_* (12 tools, 10,024 characters) |
| Next groups | vps get_* (4,316), deploy (3,453), files (3,431) |
| Free model reading time | about 1.5–3 s on a good day; a full minute when the provider is busy |

## R1: The selection strategy

**Decision**: always-on core, a one-line-per-toolset catalog, keyword prefetch before the model call, a model-callable `tools__load` as the fallback, and idle unload.

**Rationale**:
- The core covers the common phone questions in one step.
- The catalog lets the model know what exists for a few hundred tokens.
- Prefetch avoids an extra round trip in the common specialised case.
- `tools__load` makes misses recoverable within the same turn.

This is the same pattern as deferred tools and tool search in coding agents, which is proven to keep tool choice accurate while cutting context.

**Alternatives considered**:
- **Tool search over single tools**, where the model searches and gets individual schemas. It's finer-grained but costs a round trip on almost every specialised question. Toolsets match how tools are used (GitLab questions use several GitLab tools).
- **A second small model call to pick tools.** It doubles latency and the free-tier request count, which is exactly what we're reducing.
- **Embedding retrieval.** It needs an embedding model, local or remote. Keyword matching is enough for tool vocabularies, which are distinctive ("pipeline", "merge request", "rehearsal").
- **Shortening descriptions.** It changes tool servers we don't own and loses information. It's orthogonal and can be added later.

## R2: Granularity: toolsets, not single tools

**Decision**: groups of related tools, roughly 3–12 each.

**Rationale**:
- Related tools are used together: list, then read, then compare.
- Loading one tool at a time would mean many loads.
- Group descriptions are what make a short catalog possible.

**Alternatives considered**: per-tool loading (rejected, too many round trips); per-server only (too coarse: vps-mcp alone is 48 tools).

## R3: Where groups come from

**Decision**, in order of precedence:

1. core: the built-in core, global `[tools] core`, and per-server `core` patterns. Core always wins, so an operator can move one tool out of a toolset (US5);
2. per-server `toolsets` patterns in config;
3. built-in defaults for the assistant's own tools;
4. leftovers become `<server>`, or `<server>-more` when the server has configured toolsets.

Patterns are `fnmatch` globs on the server's own tool name, like the existing `allow`.

**Rationale**:
- The operator controls it.
- New servers work without configuration.
- No tool is lost.

**vps-mcp defaults** (shipped in `examples/assistant.toml`, not hard-coded, because vps-mcp is just one server):

| Group | Tools |
|---|---|
| core | `list_services`, `get_service_status`, `get_resource_usage` |
| gitlab | `gitlab_*` |
| db | `db_*` |
| deploy | `deploy_*`, `rollback_*`, `update_service`, `configure_service` |
| migration | `rehearse`, `rehearsal_status`, `cancel_rehearsal`, `run_check`, `drop_copies`, `get_report`, `list_reports`, `pipeline_last_run` |
| jobs | `*_job`, `list_jobs`, `job_status` |
| services | `start_service`, `stop_service`, `restart_service`, `set_boot_start`, `get_service_logs`, `get_service_config`, `get_audit_log`, `list_revisions` |
| memory-admin | `memory_*` |

**Built-in defaults** (in code):

| Group | Tools |
|---|---|
| core | `memory__search`, `memory__get`, `schedule__add`, `schedule__list`, `assistant__switch_model`, `tools__load` |
| files | `files__*` |
| schedules | `schedule__remove`, `schedule__pause`, `schedule__resume` |

## R4: Keyword scoring for prefetch

**Decision**:

- **Each toolset's keywords**: the stemmed words of its tool names (split on `_`) at weight 2, its configured keywords at weight 3, and words from its tools' descriptions at weight 0.5. Description words found in more than half of all toolsets are dropped. Uses agent-memory's `stem()` and `STOPWORDS`, with its own tokenizer that keeps short technical words (ci, db, mr).
- **Words ignored for matching**: generic words that appear in tool names but say nothing about which toolset is meant ("status", "list", "info", "service") and filler words ("please", "show", "last", "today"). This was found during implementation: "what's the status" otherwise loaded two toolsets, although the core already answers it.
- **The query**: the stemmed words of the current message. A first version also matched the two previous messages at half weight. The live test showed this kept a GitLab toolset loaded through four off-topic messages (about 53 s per answer on the local model instead of about 21 s), so it was dropped. Follow-ups are covered by the unload delay.
- **Score**: the sum over matched query words of keyword weight × query weight × idf, where idf = log(1 + toolsets / toolsets having the word as a strong keyword, from a name or the config). Words match when their stems are equal, or one is a prefix of the other with at least 5 characters (deploy/deployment, rehears/rehearsal).
- **Loading rule**: load toolsets scoring ≥ 2.0 and ≥ 60% of the best score, the best two at most (configurable). A single strong word such as "pipeline" qualifies on its own, and a clear winner doesn't drag in weak runners-up.
- **Measured**: 20/20 on the test questions written for SC-002; chit-chat loads nothing.

**Rationale**:
- It's deterministic, fast and explainable: the audit trail lists the matched words.
- idf stops common words ("service", "list", "status") from loading everything.
- Name tokens are the most reliable signal, so they get the higher weight.

**Alternatives considered**:
- **Exact substring matching**: misses inflections ("pipelines", "deployed").
- **BM25 over full descriptions only**: descriptions are long and noisy.

## R5: Calls to a tool that isn't loaded

**Decision**: if the name is an allowed tool from an unloaded toolset, load the toolset, audit it with the reason `called <tool>`, and handle the call normally.

**Rationale**:
- Models sometimes call a tool they saw earlier in the conversation, or one the catalog makes obvious.
- Making the model repeat the call would cost a round trip with no safety gain:
  - the policy and approvals still apply (`ask` asks, `never` tools aren't in the catalog at all);
  - a malformed call is rejected by the tool's own argument validation.
- It also keeps scripted behaviour identical with lazy loading on (SC-004).

**Alternatives considered**: returning "loaded, please repeat" without running the call. This was the first design; it was dropped during implementation because it adds a step and changes behaviour without making anything safer.

## R6: Persistence and unloading

**Decision**: the table `toolsets_active(conversation, name, loaded_turn, last_used_turn, reason)`.

- **Turn counter**: a monotonic per-conversation counter in the `kv` table. The turns table can't be used because old turns are folded away into the summary.
- **Unloading**: at the start of each turn, a toolset that was never used is unloaded after `unused_turns` (default 2). A used one is unloaded when `turn - max(loaded_turn, last_used_turn) ≥ idle_turns` (default 6). A toolset the current message matches again stays. `last_used_turn = 0` means not used yet.
- **New conversation**: `/new` and conversation end start a new conversation id, so nothing is loaded.

**Rationale**:
- Per-conversation state matches how context works.
- It survives restarts, so a restart mid-conversation doesn't lose loaded tools.

## R7: Prompt stability and caching

**Decision**:

- The catalog is rendered in the system prompt, sorted by toolset name. It depends only on the configuration, the tool names and server availability.
- The tool list is the core tools in a fixed order, then loaded toolsets in catalog order, each in a fixed tool order.

**Rationale**:
- Local servers and some providers cache the prompt prefix.
- The system prompt plus core tools stays identical between calls.
- Loading only appends after the core.

## R8: Measurement

**Decision**:

- **Model-call audit records**: the outcome gains `tools=<chars>`, the size of the tool definitions sent.
- **`/tools`**: shows the current size, the toolsets and their tool counts, and which are loaded.
- **Tests**: a quality test runs 20 fixed questions against a fixture of the real vps-mcp tool names and descriptions (public tool metadata only) and asserts the SC-002 hit rate.

**Rationale**: The improvement and the prefetch quality should both be visible in real use, not only in tests, so the keywords can be tuned from the audit trail.

## R9: Per-model setting (added after the live test)

**Decision**: `lazy_tools` on each model profile overrides `[tools] lazy`.

**Rationale**:
- A large, fast hosted model reads the full tool list in well under a second. For it, never missing a tool is worth more than the saved tokens.
- A local or free-tier model pays heavily for every token. Free tiers can also have a per-request prompt cap.
- Switching models with `/model` therefore switches the strategy too.
