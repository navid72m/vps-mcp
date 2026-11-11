# Quickstart & Validation Guide

This guide checks the feature end to end against a disposable test VPS. Tool names and shapes are defined in [contracts/mcp-tools.md](./contracts/mcp-tools.md). Config is defined in [contracts/policy-config.md](./contracts/policy-config.md).

## Prerequisites

- **Workstation**: Python 3.12+, `uv`, and an MCP client (for example Claude Code).
- **Test VPS**: Ubuntu 24.04 with systemd, Docker, and nginx. A local VM works, for example:
  - `multipass launch 24.04 --name vpsmcp-test`
  - or `tests/integration/provision.sh`, once it is implemented.
  - **Never** use a production VPS for these scenarios.
- **SSH key**: an SSH keypair for the `vpsmcp` user on the VPS.

## Setup

```bash
uv sync                                                    # install
uv run vps-mcp bootstrap --print-sudoers > vps-mcp.sudoers # review, then install on VPS:
#   sudo install -m 0440 vps-mcp.sudoers /etc/sudoers.d/vps-mcp && sudo visudo -c
uv run vps-mcp trust-host --host <vps-ip> --known-hosts ~/.config/vps-mcp/known_hosts
cp examples/vps-mcp.toml ~/.config/vps-mcp/test.toml        # edit host/key; declare nginx (os, protected) + api (container)
uv run vps-mcp check-config --config ~/.config/vps-mcp/test.toml   # expect: OK
claude mcp add vps -- uv run vps-mcp serve --config ~/.config/vps-mcp/test.toml
```

## Automated checks

```bash
uv run pytest tests/unit tests/contract               # no VPS needed
uv run pytest -m integration --vps-config ~/.config/vps-mcp/test.toml
```

**Expected**:
- All tests pass.
- The negative suite reports at least 50 cases with 0 mutating remote commands issued (SC-006).

## Manual validation scenarios

Run each prompt through the MCP client and compare the result with what you observe directly on the VPS (`systemctl`, `docker ps`).

| # | Story | Prompt / action | Expected |
|---|-------|-----------------|----------|
| 1 | US1 | "List my services" | nginx (os) and api (container) are listed with the correct state, uptime, and boot setting, in under 10 s (SC-001). |
| 2 | US1 | "Status of postgres" (not declared) | `not_permitted`. Nothing reveals whether postgres exists. |
| 3 | US2 | "Restart api" | `success`. The new start time is later than before (SC-002: < 30 s). |
| 4 | US2 | "Stop nginx" without confirming | `confirmation_required`. nginx is still running. |
| 5 | US2 | "Stop sshd" | `not_permitted` (lock-out guard). |
| 6 | US2 | Set `read_only = true`, restart the server, then "restart api" | `read_only`. Restore the setting afterwards. |
| 7 | US3 | "Last 50 lines of nginx logs containing 404" | At most 50 entries, all containing "404", in timestamp order. |
| 8 | US4 | "Deploy redis:7 container named cache" | A preview first. After confirming: running, listed in `list_services`, audit `success`. |
| 9 | US4 | "Deploy quay.io/evil/x" | `unapproved_source`. Nothing is pulled (`docker images` is unchanged). |
| 10 | US5 | "Set worker_connections to 2048 in nginx.conf" | A diff preview, then `success` after confirming. nginx is reloaded. |
| 11 | US5 | Submit an invalid nginx.conf with confirmation | Validation fails, or the health gate fails and the result is `rolled_back`. The original file is restored and nginx is running (SC-005). |
| 12 | US5 | "Roll back cache" after changing its env | The previous env is restored and the container is recreated. |
| 13 | US6 | "Remove cache, keep data" and confirm | The container is gone, the volume still exists, and `data_deleted: false`. |
| 14 | US6 | "Remove nginx" with confirmation | `protected`. Nothing changes. |
| 15 | US7 | "How loaded is the server?" | Load, memory, and disk figures agree with `uptime`, `free`, and `df` within 5%. |
| 16 | US8 | "Show the audit log" | Scenarios 3–14 appear newest first, including refused and rolled-back ones. `grep` of the DB file and the server log for the key path or passphrase finds nothing (SC-007). |
| 16a | US4 | "Deploy postgres:16 as odoo-db on network odoo-net (no ports), then odoo:18 as odoo on odoo-net with HOST=odoo-db and port 8069" | `odoo-net` is created. Both services are running. `http://<vps>:8069` shows the Odoo setup page. `ss -tlnp` on the VPS shows no 5432 listener. Removing both deletes `odoo-net` (`networks_removed`). |
| 17 | Edge | Stop the VM (`multipass stop`), then "list services" | `connection_failure/unreachable` within 15 s (SC-008). |
| 18 | Edge | Replace the VPS host key, then any call | `connection_failure/identity_mismatch`. The server does not proceed. |
