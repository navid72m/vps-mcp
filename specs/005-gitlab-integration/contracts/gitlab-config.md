# Contract: `[gitlab]` Configuration

```toml
[gitlab]
url = "https://gitlab.example.com"        # or https://gitlab.com
keychain_service = "vps-mcp-gitlab"       # macOS Keychain generic password (service)
keychain_account = "deploy-bot"           # ... and account; the token itself is never in this file
# ca_bundle = "/Users/me/.config/vps-mcp/company-ca.pem"
# timeout_s = 30
# max_archive_mb = 200

[[gitlab.project]]
alias = "addons"
id = 1234                                  # or path = "mygroup/odoo-addons"
permissions = ["read", "write_issues", "write_mrs", "ci", "deploy"]
close_issues = true                        # only issues created by vps-mcp, unless close_any_issue
trigger_refs = ["main", "release/*"]

[[gitlab.project]]
alias = "migration-scripts"
path = "mygroup/odoo-migration"
permissions = ["read", "deploy"]

[[gitlab.deploy]]
name = "addons"
project = "addons"
refs = ["tag:v*", "branch:main"]
destination = "/opt/odoo/custom-addons"    # becomes a symlink to the active release
owner = "odoo"
group = "odoo"
exclude = [".gitlab-ci.yml", "tests/**"]
services = ["odoo19"]
health_url = "http://127.0.0.1:8069/web/login"
require_pipeline_success = true
keep_releases = 3

[[gitlab.deploy]]
name = "migration-scripts"
project = "migration-scripts"
refs = ["tag:*"]
destination = "/opt/migration/scripts"
include = ["*.sh", "*.py", "sql/**"]
```

## Store the token (once, interactively)

```bash
security add-generic-password -U -s vps-mcp-gitlab -a deploy-bot -w
# prompts for the token: it never appears on a command line or in shell history
uv run vps-mcp gitlab-check --config ~/.config/vps-mcp/myvps.toml
```

## Token scopes

| Permissions declared | Minimum token scope |
|---|---|
| `read` only (including deploys of public projects) | `read_api` |
| `deploy` of a private project | `read_api` (archive download) |
| `write_issues`, `write_mrs`, `ci` | `api` |

Use a project or group access token with the lowest role that works. `gitlab-check` reports any scope that is missing for the permissions you declared.

## Validation

`check-config` checks all of the following and reports every violation:

- the rules in data-model §1;
- `deploy.project` must have the `deploy` permission;
- the services must be declared;
- destinations must be outside system paths, unique and not nested inside one another.
