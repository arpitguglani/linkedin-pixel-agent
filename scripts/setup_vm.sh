#!/usr/bin/env bash
# Non-interactive VM setup (Ubuntu 24.04 ARM64). Safe to re-run.
# Invoked automatically by cloud-init from scripts/oci_bootstrap.sh, or manually
# from the repo root:  bash scripts/setup_vm.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"
SUDO=""; [[ $EUID -ne 0 ]] && SUDO="sudo"
RUN_USER="${SUDO_USER:-${RUN_USER:-$(id -un)}}"
[[ "$RUN_USER" == "root" ]] && id ubuntu >/dev/null 2>&1 && RUN_USER=ubuntu

[[ -f .env ]] || { echo "Missing .env (cp .env.example .env and fill it in)" >&2; exit 1; }
set -a; source .env; set +a

echo "==> Docker"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | $SUDO sh
fi
$SUDO usermod -aG docker "$RUN_USER" || true

echo "==> Host firewall: allow 80/443 (Oracle images ship restrictive iptables)"
for port in 80 443; do
  $SUDO iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null ||
    $SUDO iptables -I INPUT 1 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
done
$SUDO DEBIAN_FRONTEND=noninteractive apt-get install -y -q iptables-persistent >/dev/null 2>&1 || true
$SUDO netfilter-persistent save >/dev/null 2>&1 || true

echo "==> Swap (Chromium headroom)"
if ! swapon --show | grep -q /swapfile; then
  $SUDO fallocate -l 4G /swapfile && $SUDO chmod 600 /swapfile
  $SUDO mkswap /swapfile >/dev/null && $SUDO swapon /swapfile
  echo '/swapfile none swap sw 0 0' | $SUDO tee -a /etc/fstab >/dev/null
fi

echo "==> DuckDNS updater (every 5 min)"
if [[ -n "${DUCKDNS_TOKEN:-}" && "${MCP_DOMAIN:-}" == *.duckdns.org ]]; then
  sub="${MCP_DOMAIN%.duckdns.org}"
  cat <<EOF | $SUDO tee /usr/local/bin/duckdns-update >/dev/null
#!/bin/sh
curl -fsS "https://www.duckdns.org/update?domains=${sub}&token=${DUCKDNS_TOKEN}&ip=" >/dev/null
EOF
  $SUDO chmod 700 /usr/local/bin/duckdns-update
  $SUDO /usr/local/bin/duckdns-update || true
  echo "*/5 * * * * root /usr/local/bin/duckdns-update" | $SUDO tee /etc/cron.d/duckdns >/dev/null
fi

echo "==> Data directories (containers run as uid 1000)"
mkdir -p deploy/data/{linkedin-profile,proxy/oauth,watcher,caddy}
$SUDO chown -R 1000:1000 deploy/data/linkedin-profile deploy/data/proxy deploy/data/watcher
chmod 600 .env

echo "==> Start the stack"
$SUDO docker compose --env-file .env -f deploy/compose.yaml pull -q linkedin-mcp caddy
$SUDO docker compose --env-file .env -f deploy/compose.yaml up -d --build

echo "==> Telegram /login support (host picks up the request file every minute)"
cat <<EOF | $SUDO tee /etc/cron.d/linkedin-agent-login >/dev/null
* * * * * root f=$REPO_DIR/deploy/data/watcher/login.request; [ -f \$f ] && rm -f \$f && cd $REPO_DIR && flock -n /tmp/li-login.lock bash scripts/linkedin_login.sh >>/var/log/linkedin-login.log 2>&1
EOF

echo "==> Auto-update weekly (security patches + newer linkedin-mcp image)"
cat <<EOF | $SUDO tee /etc/cron.d/linkedin-agent-update >/dev/null
30 4 * * 0 root cd $REPO_DIR && git pull -q --ff-only; docker compose --env-file .env -f deploy/compose.yaml pull -q && docker compose --env-file .env -f deploy/compose.yaml up -d --build >/dev/null 2>&1
EOF
$SUDO chown -R "$RUN_USER":"$RUN_USER" "$REPO_DIR" 2>/dev/null || true
$SUDO chown -R 1000:1000 deploy/data/linkedin-profile deploy/data/proxy deploy/data/watcher

echo "SETUP_COMPLETE"
