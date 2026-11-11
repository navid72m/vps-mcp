# Contract: `[memory]` Configuration

```toml
[memory]                     # optional; when missing, memory is on with these defaults
enabled = true
# path = "~/.config/vps-mcp/myvps.memory"   # default: next to this file, <stem>.memory
writable = true              # false: agents can read memory but not write or archive notes
briefing_chars = 6000        # connect-time briefing budget (~1,500 words)
max_note_bytes = 8192
max_notes = 500
```

**Validation** (`check-config` reports all violations):

1. `path` is expanded (`~`) and made absolute. It must not resolve inside the installed vps-mcp package or source directory.
2. Ranges are as in data-model §1.
3. Unknown keys are errors (`_Strict`).

**Warnings** (not errors):

- The resolved `path` is inside a git work tree whose remotes include a public host. This catches someone accidentally publishing memory.
- The folder exists but is group- or world-readable.

**Folder bootstrap**: on first use the server creates `notes/`, `generated/` and `archive/` (0700), plus a `README.md` explaining the layout (0600).
