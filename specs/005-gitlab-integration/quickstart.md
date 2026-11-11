# Quickstart / Validation: GitLab Integration

## Prerequisites

- A working vps-mcp setup (features 001–004).
- A GitLab access token with the scopes listed in [contracts/gitlab-config.md](./contracts/gitlab-config.md).
- A test project you control. Use a throwaway one for the write and deploy checks.

## 1. Automated checks (no GitLab, no VPS)

```bash
uv run pytest tests/unit/gitlab tests/negative/test_gitlab_disallowed.py -q
uv run ruff check && uv run mypy
```

All tests must pass. The suites run against a fake GitLab HTTP server and the existing FakeVps. They include:

- the **token-leak test** (SC-002): a known token is used for every tool and every error path, then output, logs, audit, memory files and every FakeVps command and stdin are searched for it;
- **≥ 30 refusal cases** (SC-003);
- **tar sanitising**: tar-slip, symlink escape and device entries;
- **automatic rollback** when a service is unhealthy (SC-004).

## 2. Credential setup

```bash
security add-generic-password -U -s vps-mcp-gitlab -a <account> -w     # paste token at the prompt
uv run vps-mcp check-config --config ~/.config/vps-mcp/myvps.toml
uv run vps-mcp gitlab-check --config ~/.config/vps-mcp/myvps.toml
```

`gitlab-check` should show:

- the token user;
- the scopes;
- the expiry date;
- every project as `ok (id N)`;
- for each deploy target, whether it has an active release, or `not deployed yet`.

## 3. Read (US1), in any MCP client

Ask the client:

- "List branches of addons"
- "Show models/stock.py on main"
- "What changed between v1.4 and main?"

Expected:

- compact answers, with `truncated` flagged on large items;
- a non-allowlisted path such as `project="other"` is refused as `not_permitted`.

## 4. Issues, MRs and CI (US3, US4) on the throwaway project

1. "Open an issue titled 'vps-mcp test'": a preview is shown first. After you confirm, the issue exists with the footer, and `get_audit_log` lists `gitlab_issue`.
2. "Comment 'ok' on it", then "close it": both work only with the matching permissions.
3. "Did the last pipeline on main pass? Show the failing job's log": you get a masked tail.

## 5. Deploy (US2) to a test destination

**Setup**: declare a target with destination `/opt/vps-mcp-test/app`, refs `tag:*`, and a test service. Then run:

```bash
uv run vps-mcp bootstrap --print-sudoers --with-migration --with-jobs --with-gitlab --config … > vps-mcp.sudoers
```

and install the result as root, after reviewing it.

**Steps**:

1. "Plan deploying v0.1 to the test target". Check the plan shows:
   - the commit;
   - the file list;
   - the pipeline status;
   - `unmanaged_existing_dir` if the directory already exists;
   - the services.
2. Confirm. Then check on the VPS:
   - `readlink /opt/vps-mcp-test/app` points to `/var/lib/vps-mcp/deploys/<target>/releases/…-<sha12>`;
   - `sha256sum -c MANIFEST.sha256` passes;
   - the service is active;
   - the briefing's "Deployed versions" shows v0.1.
3. Deploy v0.2, then "roll back the test target". The symlink points to the v0.1 release again.
4. **Health failure**: point `health_url` at a path that fails, then deploy. Expected: `rolled_back`, with the previous release active again in under 2 minutes.
5. **Token check**: on the VPS, check that the token never touched the box:

   ```bash
   grep -r "<first 8 chars of token>" /var/lib/vps-mcp /tmp /root/.bash_history
   ```

   It should find nothing. You can also run `ps` during a deploy and check no command line contains the token.
