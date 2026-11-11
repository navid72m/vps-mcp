#!/usr/bin/env bash
# Provision a disposable Ubuntu 24.04 VM for the integration tests (T078).
# NEVER point this at a production machine.
#
#   tests/integration/provision.sh            # creates VM "vpsmcp-test" with multipass
#   uv run pytest -m integration --vps-config tests/integration/vps-mcp.test.toml
set -euo pipefail

VM="${VM:-vpsmcp-test}"
HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/../.." && pwd)"
KEY="$HERE/.vpsmcp_test_ed25519"
KNOWN_HOSTS="$HERE/.known_hosts"
CONFIG="$HERE/vps-mcp.test.toml"

command -v multipass >/dev/null || { echo "multipass is required (brew install multipass)"; exit 1; }

if ! multipass info "$VM" >/dev/null 2>&1; then
  multipass launch 24.04 --name "$VM" --cpus 2 --memory 2G --disk 10G
fi

[ -f "$KEY" ] || ssh-keygen -t ed25519 -N "" -f "$KEY" -C vpsmcp-test >/dev/null

multipass exec "$VM" -- sudo bash -euo pipefail -c "
  export DEBIAN_FRONTEND=noninteractive
  apt-get update -q
  apt-get install -y -q nginx docker.io
  id vpsmcp >/dev/null 2>&1 || useradd -m -s /bin/bash vpsmcp
  install -d -m 700 -o vpsmcp -g vpsmcp /home/vpsmcp/.ssh
  echo '$(cat "$KEY.pub")' > /home/vpsmcp/.ssh/authorized_keys
  chown vpsmcp:vpsmcp /home/vpsmcp/.ssh/authorized_keys && chmod 600 /home/vpsmcp/.ssh/authorized_keys
  docker rm -f api >/dev/null 2>&1 || true
  docker run -d --name api --restart unless-stopped -p 8080:80 nginx:alpine >/dev/null
"

(cd "$ROOT" && uv run vps-mcp bootstrap --print-sudoers --runtime docker --pm apt) \
  | multipass exec "$VM" -- sudo bash -c 'cat > /tmp/vps-mcp.sudoers && visudo -cf /tmp/vps-mcp.sudoers && install -m 0440 /tmp/vps-mcp.sudoers /etc/sudoers.d/vps-mcp'

IP="$(multipass info "$VM" --format json | python3 -c 'import json,sys;d=json.load(sys.stdin);print(next(iter(d["info"].values()))["ipv4"][0])')"
rm -f "$KNOWN_HOSTS"
(cd "$ROOT" && uv run vps-mcp trust-host --host "$IP" --known-hosts "$KNOWN_HOSTS" --yes)

cat > "$CONFIG" <<EOF
[vps]
name = "integration"
host = "$IP"
user = "vpsmcp"
key_path = "$KEY"
known_hosts_path = "$KNOWN_HOSTS"
container_runtime = "docker"
package_manager = "apt"

[policy]
health_stabilize_s = 3
[policy.approved_sources]
apt_origins = ["Ubuntu"]
image_prefixes = ["docker.io/library/"]

[[service]]
name = "nginx"
kind = "os"
package = "nginx"
protected = true
validate = "nginx"
activate = "reload"
config_files = [{ path = "/etc/nginx/nginx.conf" }]

[[service]]
name = "api"
kind = "container"
EOF

# --- migration feature (002): PostgreSQL with two small databases differing on purpose ---
multipass exec "$VM" -- sudo bash -euo pipefail -c '
  apt-get install -y -q postgresql acl >/dev/null
  sudo -u postgres dropdb --if-exists it_after
  sudo -u postgres dropdb --if-exists it_before
  sudo -u postgres createdb it_before
  sudo -u postgres psql -q -d it_before -c "CREATE TABLE res_partner (id serial primary key, name varchar(64), vat varchar(32)); INSERT INTO res_partner(name, vat) SELECT g::text, g::text FROM generate_series(1,1000) g; ANALYZE;"
  sudo -u postgres createdb -T it_before it_after
  sudo -u postgres psql -q -d it_after -c "ALTER TABLE res_partner DROP COLUMN vat; DELETE FROM res_partner WHERE id > 600;"
'
(cd "$ROOT" && uv run vps-mcp bootstrap --print-migration-sql) \
  | multipass exec "$VM" -- sudo -u postgres psql -q
PGPORT=$(multipass exec "$VM" -- pg_lsclusters -h | awk 'NR==1{print $3}')
{
  echo ""
  echo "[migration]"
  echo "port = $PGPORT"
  echo "[[migration.target]]"
  echo 'name = "it_before"'
  echo "[[migration.target]]"
  echo 'name = "it_after"'
} >> "$CONFIG"

(cd "$ROOT" && uv run vps-mcp check-config --config "$CONFIG")
echo "Ready: uv run pytest -m integration --vps-config $CONFIG"
