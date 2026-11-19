#!/usr/bin/env bash
# (Re-)login to LinkedIn from your phone. Run on the VM:
#   sudo bash scripts/linkedin_login.sh
# It prints an https://login.<your-domain>/login.html#token=... link. Open it on
# your Pixel, sign in to LinkedIn (incl. 2FA) and wait — the viewer closes
# itself and the session is saved. Times out after 30 minutes.
set -euo pipefail
cd "$(dirname "$0")/.."
set -a; source .env; set +a
DC=(docker compose --env-file .env -f deploy/compose.yaml)

# Free CPU/RAM on small VMs (e.g. e2-micro) while the login browser runs; keep Caddy up.
"${DC[@]}" stop linkedin-mcp auth-proxy watcher >/dev/null 2>&1 || true
find "$PWD/deploy/data/linkedin-profile" -name 'Singleton*' -delete 2>/dev/null || true
trap 'find "$PWD/deploy/data/linkedin-profile" -name "Singleton*" -delete 2>/dev/null || true; "${DC[@]}" up -d linkedin-mcp auth-proxy watcher >/dev/null 2>&1 || true' EXIT

url=""
for attempt in 1 2 3; do
  docker rm -f linkedin-login >/dev/null 2>&1 || true
  docker run -d --name linkedin-login \
    --hostname "$(hostname)" \
    -e TZ="${DAILY_DIGEST_TZ:-America/Los_Angeles}" \
    -v "$PWD/deploy/data/linkedin-profile:/home/pwuser/.linkedin-mcp" \
    -p 127.0.0.1:6080:6080 --shm-size=1g \
    stickerdaniel/linkedin-mcp-server:latest --login --login-viewer >/dev/null

  for _ in $(seq 1 90); do
    url="$(docker logs linkedin-login 2>&1 | grep -oE 'http://127\.0\.0\.1:6080/[^[:space:]]+' | tail -1 || true)"
    [[ -n "$url" ]] && break
    state="$(docker inspect -f '{{.State.Status}}' linkedin-login 2>/dev/null || echo exited)"
    [[ "$state" != "running" ]] && break
    sleep 2
  done
  [[ -n "$url" ]] && break
  echo "Login viewer startup attempt $attempt/3 failed; retrying..." >&2
  sleep 3
done

if [[ -z "$url" ]]; then
  echo "Login viewer did not start:" >&2
  docker logs --tail 40 linkedin-login >&2
  exit 1
fi

public="${url/http:\/\/127.0.0.1:6080/https://login.${MCP_DOMAIN}}"
echo
echo "================================================================"
echo " Open this on your phone and sign in to LinkedIn:"
echo "   $public"
echo "================================================================"
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=🔐 LinkedIn login (valid 30 min): ${public}" >/dev/null || true
  echo " (also sent to your Telegram)"
fi
echo "Waiting for you to finish..."
code="$(docker wait linkedin-login)"
docker logs --tail 5 linkedin-login 2>&1 || true
docker rm linkedin-login >/dev/null 2>&1 || true
[[ "$code" == "0" ]] && echo "✅ LinkedIn session saved; server restarted." || { echo "Login exited with $code" >&2; exit "$code"; }
