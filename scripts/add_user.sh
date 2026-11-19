#!/usr/bin/env bash
# Provision an isolated LinkedIn MCP + OAuth proxy stack for an additional user
# on the same VM (shares Caddy + GCP OAuth client, separate LinkedIn profile & ports).
#
# Usage (on the VM):
#   sudo bash scripts/add_user.sh <short_name> <google_email> [domain] [slot]
# Example:
#   sudo bash scripts/add_user.sh wife wife@gmail.com wife-li.duckdns.org
#   sudo bash scripts/add_user.sh wife wife@gmail.com   # defaults to wife.<MCP_DOMAIN>
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $# -lt 2 ]]; then
  echo "Usage: sudo bash scripts/add_user.sh <short_name> <google_email> [domain] [slot]" >&2
  exit 1
fi

USER_ID="$(echo "$1" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')"
USER_EMAIL="$2"
set -a; source .env; set +a
USER_DOMAIN="${3:-${USER_ID}.${MCP_DOMAIN}}"
SLOT="${4:-1}"

if [[ -z "$USER_ID" || "$SLOT" -lt 1 || "$SLOT" -gt 20 ]]; then
  echo "Invalid short_name or slot (slot must be 1..20)" >&2
  exit 1
fi

MCP_PORT=$((8000 + SLOT))
PROXY_PORT=$((8080 + SLOT))
LOGIN_PORT=$((6080 + SLOT))

UDIR="$PWD/deploy/users/$USER_ID"
mkdir -p "$UDIR/data/linkedin-profile" "$UDIR/data/proxy/oauth" "$PWD/deploy/caddy/users.d"
chown -R 1000:1000 "$UDIR/data/linkedin-profile" "$UDIR/data/proxy"
chmod 700 "$UDIR/data/linkedin-profile"

if [[ ! -f "$UDIR/.env" ]]; then
  JWT_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(48))')"
  ENC_KEY="$(python3 -c 'import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())')"
  cat >"$UDIR/.env" <<EOF
MCP_DOMAIN=${USER_DOMAIN}
GOOGLE_CLIENT_ID=${GOOGLE_CLIENT_ID}
GOOGLE_CLIENT_SECRET=${GOOGLE_CLIENT_SECRET}
ALLOWED_EMAIL=${USER_EMAIL}
JWT_SIGNING_KEY=${JWT_KEY}
STORAGE_ENCRYPTION_KEY=${ENC_KEY}
WRITE_COOLDOWN_SECONDS=${WRITE_COOLDOWN_SECONDS:-60}
WRITE_DAILY_CAP=${WRITE_DAILY_CAP:-20}
DAILY_DIGEST_TZ=${DAILY_DIGEST_TZ:-America/Los_Angeles}
MCP_PORT=${MCP_PORT}
PROXY_PORT=${PROXY_PORT}
LOGIN_PORT=${LOGIN_PORT}
EOF
  chmod 600 "$UDIR/.env"
else
  sed -i "s|^MCP_DOMAIN=.*|MCP_DOMAIN=${USER_DOMAIN}|; s|^ALLOWED_EMAIL=.*|ALLOWED_EMAIL=${USER_EMAIL}|" "$UDIR/.env"
fi

cat >"$UDIR/compose.yaml" <<EOF
name: linkedin-agent-${USER_ID}

services:
  linkedin-mcp:
    image: stickerdaniel/linkedin-mcp-server:latest
    restart: unless-stopped
    network_mode: host
    environment:
      TZ: \${DAILY_DIGEST_TZ:-America/Los_Angeles}
    volumes:
      - ./data/linkedin-profile:/home/pwuser/.linkedin-mcp
    command: ["--transport", "streamable-http", "--host", "127.0.0.1", "--port", "${MCP_PORT}", "--path", "/mcp"]
    shm_size: "1gb"

  auth-proxy:
    image: linkedin-pixel-agent-auth-proxy:latest
    restart: unless-stopped
    network_mode: host
    env_file: ./.env
    environment:
      LINKEDIN_MCP_URL: http://127.0.0.1:${MCP_PORT}/mcp
      OAUTH_STORAGE_DIR: /data/oauth
      PROXY_HOST: 127.0.0.1
      PROXY_PORT: "${PROXY_PORT}"
    volumes:
      - ./data/proxy:/data
    command: ["python", "-m", "auth_proxy.proxy"]
    depends_on: [linkedin-mcp]
EOF

cat >"$PWD/deploy/caddy/users.d/${USER_ID}.caddy" <<EOF
${USER_DOMAIN} {
	encode gzip
	handle {
		reverse_proxy 127.0.0.1:${PROXY_PORT} {
			flush_interval -1
		}
	}
	header {
		Strict-Transport-Security "max-age=31536000"
		-Server
	}
}

login.${USER_DOMAIN} {
	reverse_proxy 127.0.0.1:${LOGIN_PORT}
}
EOF

# Ensure the auth-proxy image exists and Caddy mounts users.d
docker compose --env-file .env -f deploy/compose.yaml up -d caddy >/dev/null
docker exec linkedin-pixel-agent-caddy-1 caddy reload --config /etc/caddy/Caddyfile >/dev/null 2>&1 || \
  docker compose --env-file .env -f deploy/compose.yaml restart caddy >/dev/null

docker compose --env-file "$UDIR/.env" -f "$UDIR/compose.yaml" up -d

cat <<TXT

✅ User '${USER_ID}' is ready on this VM!
   MCP URL:      https://${USER_DOMAIN}/mcp
   Redirect URI: https://${USER_DOMAIN}/auth/callback
   Allowed user: ${USER_EMAIL}

Next steps when ${USER_ID} is ready:
  1. In GCP Console -> Auth -> Audience -> Test users: add ${USER_EMAIL}
  2. In GCP Console -> Credentials -> OAuth client -> Authorized redirect URIs:
       add https://${USER_DOMAIN}/auth/callback
  3. Run LinkedIn login for ${USER_ID}:
       sudo bash scripts/user_login.sh ${USER_ID}
  4. Connect Claude / Gemini to: https://${USER_DOMAIN}/mcp
TXT
