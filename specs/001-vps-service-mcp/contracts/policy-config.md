# Contract: Policy & Connection Config (TOML)

The operator writes this file, and the server only ever reads it. `vps-mcp check-config --config <path>` validates it and exits non-zero on any error. The server refuses to start with an invalid config.

The field rules are defined in [data-model.md §1](../data-model.md#1-configuration-entities-policy-file).

## Example

```toml
[vps]
name = "prod-1"
host = "203.0.113.10"
port = 22
user = "vpsmcp"
key_path = "~/.ssh/vps_mcp_ed25519"      # omit to use ssh-agent
known_hosts_path = "~/.config/vps-mcp/known_hosts"
container_runtime = "docker"              # docker | podman | none
package_manager = "apt"                   # apt | dnf
# allow_root = false

[policy]
read_only = false
min_free_disk_mb = 1024
log_default_lines = 200
audit_retention_days = 90
revisions_kept = 5
secret_keys = ["*PASSWORD*", "*SECRET*", "*TOKEN*", "*KEY*"]
remote_access_units = ["ssh", "sshd"]

[policy.timeouts]
control_s = 60
deploy_s = 600
query_s = 15

[policy.approved_sources]
apt_origins = ["Ubuntu", "nginx"]
image_prefixes = ["docker.io/library/", "ghcr.io/myorg/"]

[[service]]
name = "nginx"
kind = "os"
package = "nginx"
protected = true
validate = "nginx"
activate = "reload"
config_files = [
  { path = "/etc/nginx/nginx.conf", owner = "root", group = "root", mode = "0644" },
]

[[service]]
name = "api"
kind = "container"
description = "Public API"
data_paths = []          # for containers, named volumes come from the container spec
```

## Validation rules

1. `vps.known_hosts_path` must exist and contain a key for `vps.host`. Otherwise the server fails with a hint to run `vps-mcp trust-host`.
2. `user = "root"` requires `allow_root = true`. When both are set, the server logs a warning at startup.
3. Each `(name, kind)` pair must be unique.
4. A `[[service]]` entry may not list a unit that appears in `remote_access_units`.
5. `config_files` and `validate` are allowed only when `kind = "os"`. `validate` must be a key in the built-in catalogue.
6. Each `config_files[].path` and each `data_paths` entry must be an absolute path, contain no `..`, and must not be under `/proc`, `/sys`, or `/dev`.
7. Unknown keys are an error, so typos cannot silently weaken the policy.
