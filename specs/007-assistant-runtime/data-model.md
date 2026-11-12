# Data Model: Personal Assistant Runtime

## 1. Configuration (`~/.config/assistant/assistant.toml`)

```toml
[assistant]
name = "Assistant"
timezone = "Europe/Berlin"
state_dir = "~/.local/state/assistant"        # default; $ASSISTANT_HOME overrides
heartbeat = "daily 21:00"                      # or "" to disable

[telegram]
allowed_user_ids = [123456789]                 # required, at least 1
offline_max_age_min = 60

[secrets]
provider = "keychain"                          # keychain | file
service = "vps-mcp-assistant"                  # keychain service name
# file = "~/.config/assistant/secrets.toml"    # provider = "file" (0600)

[model]
provider = "anthropic"
name = "claude-sonnet-5"
max_tokens = 2048
prices = { input_per_mtok = 3.0, output_per_mtok = 15.0 }

[limits]
turn_tool_calls = 12
turn_model_calls = 8
turn_tokens = 60000
day_tokens = 1000000
day_tool_calls = 300
day_cost = 5.0
approval_minutes = 10
history_turns = 12
idle_minutes = 30
max_proposals = 5

[memory]
enabled = true
store = ""                                     # default: agent-memory default store
project = "personal"
allow_scopes = ["project:vps-*", "environment:*"]

[[server]]
name = "vps"
command = ["/abs/path/uv", "run", "--directory", "/abs/path/vps-mcp", "vps-mcp", "serve",
           "--config", "/abs/path/.config/vps-mcp/myvps.toml"]
allow = ["*"]                                  # tool globs exposed to the model
policy = { restart_service = "ask", remove_service = "never", deploy_from_gitlab = "ask" }

[[schedule]]
name = "morning-briefing"
cadence = "weekdays 08:00"
prompt = "Morning briefing: server health, anything that failed overnight, open issues."
```

### Validation rules

**Telegram**
- `allowed_user_ids`: 1–10 positive integers.

**Assistant**
- `timezone`: must be a valid IANA zone.

**Servers**
- `name`: must match `^[a-z][a-z0-9_]{0,15}$` and be unique.
- `command`: a non-empty list of strings. The first item must be an absolute path or a command name found on `PATH`.
- `policy` values: one of `auto`, `ask` or `never`.
- Policy keys and `allow` entries: tool-name globs.

**Schedules**
- `cadence`: matches `^(daily|weekdays) ([01]\d|2[0-3]):[0-5]\d$` or `^every ([1-9]|1\d|2[0-4])h$`.
- `prompt`: 1–2000 characters.
- `name`: a unique slug.

**Limits**
- Every limit must be a positive number.
- `approval_minutes`: 1–60.

**General**
- Unknown keys are errors.

## 2. State DB (`<state_dir>/assistant.db`, mode 0600)

```sql
CREATE TABLE kv (key TEXT PRIMARY KEY, value TEXT);            -- tg_offset, last_heartbeat, paused, ...
CREATE TABLE conversations (
  id TEXT PRIMARY KEY, chat_id INTEGER, started TEXT, last_activity TEXT,
  status TEXT,                      -- active | ended
  summary TEXT                      -- rolling summary of folded turns
);
CREATE TABLE turns (
  id INTEGER PRIMARY KEY, conversation TEXT, ts TEXT, role TEXT,  -- user | assistant
  text TEXT,                        -- bounded (<= 8 KB); tool results are NOT stored
  tool_summary TEXT                 -- e.g. "vps__list_services ok; vps__restart_service approved"
);
CREATE TABLE approvals (
  id TEXT PRIMARY KEY, conversation TEXT, server TEXT, tool TEXT, args_hash TEXT,
  args_json TEXT,                   -- masked
  preview TEXT,                     -- masked, capped
  created TEXT, expires TEXT, message_id INTEGER,
  status TEXT                       -- pending | approved | rejected | expired | voided | executed
);
CREATE TABLE schedules (
  name TEXT PRIMARY KEY, cadence TEXT, prompt TEXT, enabled INTEGER,
  last_run TEXT, next_run TEXT, source TEXT   -- config | chat
);
CREATE TABLE budgets (day TEXT PRIMARY KEY, tokens_in INTEGER, tokens_out INTEGER,
                      tool_calls INTEGER, model_calls INTEGER, cost REAL);
CREATE TABLE proposals (
  id TEXT PRIMARY KEY, conversation TEXT, payload_json TEXT, message_id INTEGER,
  status TEXT                       -- pending | saved | discarded | expired
);
CREATE TABLE audit (
  id INTEGER PRIMARY KEY, ts TEXT, conversation TEXT, origin TEXT, kind TEXT,
  server TEXT, tool TEXT, target TEXT, outcome TEXT, tokens_in INTEGER, tokens_out INTEGER,
  cost REAL
);
```

### Approval state transitions

```text
pending ─approve(valid, not expired)─▶ approved ─executed─▶ executed
pending ─reject─▶ rejected
pending ─timer─▶ expired
pending ─/stop | /new | restart─▶ voided
```

### Conversation state transitions

```text
active ─/new | /done | idle > idle_minutes─▶ ended (→ proposals)
```

## 3. Tool naming exposed to the model

| Source | Model-facing name |
|---|---|
| MCP server tool | `<server>__<tool>`, e.g. `vps__list_services` |
| Built-in memory tools | `memory__search`, `memory__get` (always `auto`) |

## 4. Tool result envelope given to the model

```json
{"kind":"tool_output","server":"vps","tool":"list_services","untrusted":true,
 "ok":true,"content":"…compact JSON or text, capped at 12 KB…"}
```

Declined calls get `{"kind":"tool_output","declined":"rejected by the operator"}`.

## 5. Callback data (Telegram inline buttons)

`ap:<approval_id>:<a|r>:<hmac8>` · `pr:<proposal_id>:<s|d>:<hmac8>` · `ms:<schedule>:<run>:<hmac8>`

Every value is at most 64 bytes. The HMAC key is random per process, so buttons from before a restart are rejected.
