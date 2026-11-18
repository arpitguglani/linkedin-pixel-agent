#!/usr/bin/env bash
# One-time setup for an Oracle Cloud Always Free Ampere A1 VM (Ubuntu 24.04 ARM64).
# Run as the default `ubuntu` user from the repo root:  bash scripts/setup_vm.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
cd "$REPO_DIR"

if [[ ! -f .env ]]; then
  echo "Create .env first: cp .env.example .env && nano .env" >&2
  exit 1
fi
set -a; source .env; set +a

echo "==> Installing Docker Engine + Compose plugin"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi

echo "==> Opening ports 80/443 in the host firewall (Oracle images ship iptables rules)"
for port in 80 443; do
  if ! sudo iptables -C INPUT -p tcp --dport "$port" -j ACCEPT 2>/dev/null; then
    sudo iptables -I INPUT 6 -m state --state NEW -p tcp --dport "$port" -j ACCEPT
  fi
done
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y iptables-persistent >/dev/null
sudo netfilter-persistent save
echo "    Also add Ingress rules for TCP 80 and 443 in the OCI VCN Security List!"

echo "==> 4 GB swap (helps Chromium on small VMs)"
if ! swapon --show | grep -q /swapfile; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab >/dev/null
fi

echo "==> DuckDNS updater (every 5 min)"
if [[ -n "${DUCKDNS_TOKEN:-}" && "${MCP_DOMAIN:-}" == *.duckdns.org ]]; then
  sub="${MCP_DOMAIN%.duckdns.org}"
  mkdir -p "$HOME/duckdns"
  cat >"$HOME/duckdns/duck.sh" <<EOF
#!/usr/bin/env bash
curl -fsS "https://www.duckdns.org/update?domains=${sub}&token=${DUCKDNS_TOKEN}&ip=" -o "$HOME/duckdns/duck.log"
EOF
  chmod 700 "$HOME/duckdns/duck.sh"
  "$HOME/duckdns/duck.sh"
  ( crontab -l 2>/dev/null | grep -v duckdns/duck.sh; echo "*/5 * * * * $HOME/duckdns/duck.sh >/dev/null 2>&1" ) | crontab -
fi

echo "==> Data directories"
mkdir -p deploy/data/{linkedin-profile,proxy/oauth,watcher,caddy}

echo
echo "Done. Log out and back in (docker group), then:"
echo "  1) bash scripts/linkedin_login.sh      # one-time LinkedIn login"
echo "  2) docker compose --env-file .env -f deploy/compose.yaml up -d --build"
