# Contract: Assistant Interface (chat commands, CLI, internal protocols)

## Chat commands (Telegram, operator only)

| Command | Behaviour | Uses the model |
|---|---|---|
| any text | Starts a turn in the current conversation, or a new one if there is none | yes |
| `/new` | Ends the current conversation (memory proposals are offered) and starts a fresh one; voids pending approvals | no |
| `/done` | Ends the conversation; offers up to `max_proposals` memory proposals, each with Save / Discard | yes (one extraction call) |
| `/status` | Uptime, last heartbeat, servers (up/down, tool counts), paused state, pending approvals, next schedules | no |
| `/usage` | Today's tokens in/out, tool calls, model calls, estimated cost, remaining budget | no |
| `/schedules` | Lists schedules with their next run; buttons to pause, resume and run now | no |
| `/schedule add <name> <cadence> <prompt>` · `pause <name>` · `resume <name>` · `remove <name>` | Changes take effect immediately | no |
| `/stop` | Kill switch: cancels the running turn, voids approvals, refuses new turns. Persisted, so it survives restarts | no |
| `/resume` | Lifts `/stop` | no |
| `/forget` | Deletes stored conversation history; asks for confirmation first | no |

### Approval message

```text
🔐 Approve this action?
vps · restart_service
service: odoo
Preview:
<tool's own preview, masked, ≤ 2,500 chars>
Expires in 10 min.          [✅ Approve] [❌ Reject]
```

After a decision the message is edited to read "✅ Approved · done", "❌ Rejected", "⌛ Expired" or "⛔ Voided".

## CLI (`assistant`)

```text
assistant run                       # foreground: what the background service runs
assistant check                     # validates config, secrets present, servers start + list tools
assistant service install|uninstall|status [--systemd-user]
assistant audit [--since 1d] [--json]
assistant forget --all
assistant secrets show-commands     # prints the `security add-generic-password ... -w` commands
```

**Exit codes**:

| Code | Meaning |
|---|---|
| 0 | ok |
| 1 | config error |
| 2 | missing secret |
| 3 | already running |

## Internal protocols (Python)

```python
class Provider(Protocol):
    async def complete(self, *, system: str, messages: list[Msg], tools: list[ToolSpec],
                       max_tokens: int) -> Reply: ...
# Reply: blocks [Text | ToolUse(id, name, input)], usage(in, out), stop_reason

class Channel(Protocol):
    async def updates(self) -> AsyncIterator[Update]: ...            # Message | Callback
    async def send(self, chat_id: int, text: str, buttons: list[Button] | None = None) -> int
    async def edit(self, chat_id: int, message_id: int, text: str, buttons=None) -> None
    async def answer_callback(self, callback_id: str, text: str = "") -> None
    async def typing(self, chat_id: int) -> None

class SecretProvider(Protocol):
    def get(self, name: str) -> str: ...           # raises SecretMissing(name, hint)

class ToolHub:
    async def tools(self) -> list[ToolSpec]         # allowlisted, with effective policy
    async def call(self, name: str, args: dict, *, origin: str) -> ToolOutcome
    # ToolOutcome: result | declined | refused, with audit already written
```
