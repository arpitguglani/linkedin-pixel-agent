#!/usr/bin/env bash
# (Re-)login to LinkedIn for an additional user created by scripts/add_user.sh.
# Usage (on the VM):
#   sudo bash scripts/user_login.sh <short_name>
set -euo pipefail
cd "$(dirname "$0")/.."

if [[ $# -lt 1 ]]; then
  echo "Usage: sudo bash scripts/user_login.sh <short_name>" >&2
  exit 1
fi

USER_ID="$(echo "$1" | tr '[:upper:]' '[:lower:]' | tr -cd 'a-z0-9-')"
UDIR="$PWD/deploy/users/$USER_ID"
if [[ ! -f "$UDIR/.env" || ! -f "$UDIR/compose.yaml" ]]; then
  echo "User '$USER_ID' not found in $UDIR. Run scripts/add_user.sh first." >&2
  exit 1
fi

set -a; source "$UDIR/.env"; set +a
DC=(docker compose --env-file "$UDIR/.env" -f "$UDIR/compose.yaml")
CONTAINER="linkedin-login-${USER_ID}"
PORT="${LOGIN_PORT:-6081}"

"${DC[@]}" stop linkedin-mcp >/dev/null 2>&1 || true
find "$UDIR/data/linkedin-profile" -name 'Singleton*' -delete 2>/dev/null || true
trap 'find "$UDIR/data/linkedin-profile" -name "Singleton*" -delete 2>/dev/null || true; "${DC[@]}" up -d linkedin-mcp >/dev/null 2>&1 || true' EXIT

url=""
for attempt in 1 2 3; do
  docker rm -f "$CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$CONTAINER" \
    --hostname "$(hostname)" \
    -e TZ="${DAILY_DIGEST_TZ:-America/Los_Angeles}" \
    -e DISPLAY=":${DISPLAY_NUM:-100}" \
    -v "$UDIR/data/linkedin-profile:/home/pwuser/.linkedin-mcp" \
    -p "127.0.0.1:${PORT}:6080" --shm-size=1g \
    stickerdaniel/linkedin-mcp-server:latest --login --login-viewer >/dev/null

  for _ in $(seq 1 90); do
    url="$(docker logs "$CONTAINER" 2>&1 | grep -oE 'http://127\.0\.0\.1:6080/[^[:space:]]+' | tail -1 || true)"
    [[ -n "$url" ]] && break
    state="$(docker inspect -f '{{.State.Status}}' "$CONTAINER" 2>/dev/null || echo exited)"
    [[ "$state" != "running" ]] && break
    sleep 2
  done
  [[ -n "$url" ]] && break
  echo "Login viewer startup attempt $attempt/3 failed; retrying..." >&2
  sleep 3
done

if [[ -z "$url" ]]; then
  echo "Login viewer did not start:" >&2
  docker logs --tail 40 "$CONTAINER" >&2
  exit 1
fi

public="${url/http:\/\/127.0.0.1:6080/https://login.${MCP_DOMAIN}}"
echo
echo "================================================================"
echo " Open this on ${USER_ID}'s phone/laptop and sign in to LinkedIn:"
echo "   $public"
echo "================================================================"
echo "Waiting for ${USER_ID} to finish..."
code="$(docker wait "$CONTAINER")"
docker logs --tail 5 "$CONTAINER" 2>&1 || true
docker rm "$CONTAINER" >/dev/null 2>&1 || true
[[ "$code" == "0" ]] && echo "✅ LinkedIn session saved for ${USER_ID}; server restarted." || { echo "Login exited with $code" >&2; exit "$code"; }
