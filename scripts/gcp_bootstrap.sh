#!/usr/bin/env bash
# =============================================================================
#  One-shot installer for Google Cloud Always Free (e2-micro in us-west1).
#  Run inside Google Cloud Shell (https://shell.cloud.google.com):
#
#    bash <(curl -fsSL https://raw.githubusercontent.com/arpitguglani/linkedin-pixel-agent/main/scripts/gcp_bootstrap.sh)
#
#  Always Free limits respected:
#    - Machine: e2-micro in us-west1-b (Oregon)
#    - Disk:    30 GB pd-standard (standard persistent disk)
#    - Network: Standard egress (<1 GB/mo for our traffic), free ephemeral IP
#  Re-running is safe: existing resources and answers are reused.
# =============================================================================
set -euo pipefail

REPO="${REPO:-arpitguglani/linkedin-pixel-agent}"
BRANCH="${BRANCH:-main}"
NAME="linkedin-agent"
ZONE="${GCP_ZONE:-us-west1-b}"
STATE_DIR="$HOME/.linkedin-agent-gcp"
ENV_FILE="$STATE_DIR/.env"
mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ask() {
  local var="$1" prompt="$2" def="${3:-}" secret="${4:-}" cur="${!1:-}"
  [[ -n "$cur" ]] && return 0
  local p="$prompt"; [[ -n "$def" ]] && p="$prompt [$def]"
  if [[ -n "$secret" ]]; then read -r -s -p "$p: " val; echo; else read -r -p "$p: " val; fi
  val="${val:-$def}"
  [[ -z "$val" ]] && { echo "  (required)"; ask "$@"; return; }
  printf -v "$var" '%s' "$val"
}
save_env() {
  touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  grep -v "^$1=" "$ENV_FILE" >"$ENV_FILE.tmp" || true
  printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
}
rand() { python3 -c "import secrets;print(secrets.token_urlsafe(48))"; }
fernet() { python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }

# shellcheck disable=SC1090
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

# --------------------------------------------------------------------------
bold "Step 1/5 — Google Cloud project & your accounts"
PROJECT="${GCP_PROJECT:-$(gcloud config get-value project 2>/dev/null || true)}"
if [[ -z "$PROJECT" || "$PROJECT" == "(unset)" ]]; then
  echo "Available projects:"
  gcloud projects list --format='table(projectId,name)'
  ask PROJECT "GCP Project ID to use"
fi
gcloud config set project "$PROJECT" >/dev/null
echo "  Using GCP project: $PROJECT"

DEFAULT_EMAIL="$(gcloud config get-value account 2>/dev/null || true)"
ask ALLOWED_EMAIL "Your Google account email (the only one allowed in)" "$DEFAULT_EMAIL"
if [[ -z "${TELEGRAM_BOT_TOKEN+x}" ]]; then
  read -r -s -p "Telegram bot token from @BotFather (Enter to skip Telegram): " TELEGRAM_BOT_TOKEN; echo
fi
if [[ -z "${GEMINI_API_KEY+x}" ]]; then
  read -r -s -p "Gemini API key (Enter to skip — not needed for Gemini/Claude app): " GEMINI_API_KEY; echo
fi
ask DAILY_DIGEST_HOUR "Daily digest hour (0-23, local)" "18"
ask DAILY_DIGEST_TZ "Your timezone" "America/Los_Angeles"

JWT_SIGNING_KEY="${JWT_SIGNING_KEY:-$(rand)}"
STORAGE_ENCRYPTION_KEY="${STORAGE_ENCRYPTION_KEY:-$(fernet)}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(rand)}"
for k in ALLOWED_EMAIL TELEGRAM_BOT_TOKEN GEMINI_API_KEY DAILY_DIGEST_HOUR DAILY_DIGEST_TZ \
  JWT_SIGNING_KEY STORAGE_ENCRYPTION_KEY WEBHOOK_SECRET; do save_env "$k" "${!k}"; done

if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -z "${TELEGRAM_CHAT_ID:-}" ]]; then
  bot="$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getMe" | jq -r .result.username)"
  echo; echo "Open https://t.me/${bot} on your phone and press START (or send 'hi')..."
  for _ in $(seq 1 90); do
    TELEGRAM_CHAT_ID="$(curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/getUpdates" |
      jq -r '[.result[] | .message.chat.id // empty] | last // empty')"
    [[ -n "$TELEGRAM_CHAT_ID" ]] && break
    sleep 2
  done
  [[ -z "$TELEGRAM_CHAT_ID" ]] && { echo "Didn't see a message to the bot; re-run to retry." >&2; exit 1; }
  save_env TELEGRAM_CHAT_ID "$TELEGRAM_CHAT_ID"
  curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=✅ Connected. Setting up your LinkedIn agent on GCP…" >/dev/null
  echo "  Telegram connected (chat ${TELEGRAM_CHAT_ID})."
fi

# --------------------------------------------------------------------------
bold "Step 2/5 — Always Free e2-micro VM ($ZONE)"
gcloud services enable compute.googleapis.com --quiet

if ! gcloud compute firewall-rules describe "${NAME}-http-https" >/dev/null 2>&1; then
  gcloud compute firewall-rules create "${NAME}-http-https" \
    --allow=tcp:80,tcp:443 --target-tags="$NAME" --quiet >/dev/null
fi

STARTUP="$STATE_DIR/startup.sh"
cat >"$STARTUP" <<EOF
#!/bin/bash
set -e
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile && chmod 600 /swapfile
  mkswap /swapfile && swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
if [ ! -f /var/lib/linkedin-agent-ready ]; then
  apt-get update -q && apt-get install -y -q git curl jq
  curl -fsSL https://get.docker.com | sh
  git clone -q -b ${BRANCH} https://github.com/${REPO}.git /opt/linkedin-pixel-agent
  touch /var/lib/linkedin-agent-ready
fi
EOF

if ! gcloud compute instances describe "$NAME" --zone="$ZONE" >/dev/null 2>&1; then
  gcloud compute instances create "$NAME" \
    --zone="$ZONE" \
    --machine-type=e2-micro \
    --image-family=ubuntu-2404-lts-amd64 \
    --image-project=ubuntu-os-cloud \
    --boot-disk-size=30GB \
    --boot-disk-type=pd-standard \
    --tags="$NAME" \
    --metadata-from-file="startup-script=$STARTUP" \
    --quiet >/dev/null
fi

IP="$(gcloud compute instances describe "$NAME" --zone="$ZONE" \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)')"
MCP_DOMAIN="${IP//./-}.sslip.io"
save_env MCP_DOMAIN "$MCP_DOMAIN"
echo "  VM running at $IP (hostname: $MCP_DOMAIN)"

# --------------------------------------------------------------------------
bold "Google sign-in client (lets Gemini/Claude sign in as you)"
if [[ -z "${GOOGLE_CLIENT_ID:-}" ]]; then
cat <<TXT
  1. https://console.cloud.google.com/auth/branding?project=${PROJECT}
     -> Get started: app name "LinkedIn agent", your email, Audience = External -> Create
  2. https://console.cloud.google.com/auth/audience?project=${PROJECT}
     -> Test users -> Add ${ALLOWED_EMAIL}
  3. https://console.cloud.google.com/auth/clients/create?project=${PROJECT}
     -> Web application
     Authorized redirect URI:  https://${MCP_DOMAIN}/auth/callback
     -> Create, copy Client ID + Client secret
TXT
fi
ask GOOGLE_CLIENT_ID "Google OAuth Client ID"
ask GOOGLE_CLIENT_SECRET "Google OAuth Client secret" "" secret
save_env GOOGLE_CLIENT_ID "$GOOGLE_CLIENT_ID"
save_env GOOGLE_CLIENT_SECRET "$GOOGLE_CLIENT_SECRET"

# --------------------------------------------------------------------------
bold "Step 3/5 — installing on the VM (4-7 minutes on e2-micro)"
SSH=(gcloud compute ssh "$NAME" --zone="$ZONE" --quiet --command)
for _ in $(seq 1 60); do "${SSH[@]}" "true" >/dev/null 2>&1 && break; sleep 5; done
for _ in $(seq 1 120); do
  "${SSH[@]}" "test -f /var/lib/linkedin-agent-ready" >/dev/null 2>&1 && break
  sleep 5
done
gcloud compute scp "$ENV_FILE" "${NAME}:/tmp/linkedin-agent.env" --zone="$ZONE" --quiet
"${SSH[@]}" "sudo mv /tmp/linkedin-agent.env /opt/linkedin-pixel-agent/.env && sudo chmod 600 /opt/linkedin-pixel-agent/.env && cd /opt/linkedin-pixel-agent && sudo git pull -q --ff-only || true && sudo bash scripts/setup_vm.sh" | tail -n 5

for _ in $(seq 1 60); do
  curl -fsS "https://${MCP_DOMAIN}/webhook/health" >/dev/null 2>&1 && break; sleep 5
done
curl -fsS "https://${MCP_DOMAIN}/webhook/health" >/dev/null && echo "  HTTPS is live: https://${MCP_DOMAIN}" ||
  echo "  (HTTPS not reachable yet — certificate may take another minute)"

# --------------------------------------------------------------------------
bold "Step 4/5 — LinkedIn login"
if "${SSH[@]}" "sudo test -d /opt/linkedin-pixel-agent/deploy/data/linkedin-profile/profile" >/dev/null 2>&1; then
  echo "  Existing LinkedIn session found; skipping."
else
  gcloud compute ssh "$NAME" --zone="$ZONE" --quiet -- -t \
    "cd /opt/linkedin-pixel-agent && sudo bash scripts/linkedin_login.sh"
fi

# --------------------------------------------------------------------------
bold "Step 5/5 — last two clicks"
GS="$STATE_DIR/Code.gs"
curl -fsSL "https://raw.githubusercontent.com/${REPO}/${BRANCH}/gmail_trigger/Code.gs" |
  sed -e "s|__WEBHOOK_URL__|https://${MCP_DOMAIN}/webhook/gmail|" -e "s|__WEBHOOK_SECRET__|${WEBHOOK_SECRET}|" >"$GS"
cat <<TXT
A) Gmail trigger: open https://script.google.com/home/projects/create
   Replace the editor contents with everything between the lines below,
   choose function "install" -> Run -> approve permissions.
----------------------------------------------------------------------
$(cat "$GS")
----------------------------------------------------------------------
   (Also saved at $GS)

B) Phone assistant:
   Gemini: gemini.google.com (Desktop site) -> Settings -> Connected apps -> Add custom app
           URL: https://${MCP_DOMAIN}/mcp   then sign in as ${ALLOWED_EMAIL}
   Claude: claude.ai -> Settings -> Connectors -> Add custom connector
           URL: https://${MCP_DOMAIN}/mcp

Done!
TXT
if [[ -n "${TELEGRAM_BOT_TOKEN:-}" && -n "${TELEGRAM_CHAT_ID:-}" ]]; then
  curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
    --data-urlencode "text=🎉 Server is up on GCP. Add https://${MCP_DOMAIN}/mcp in Gemini → Connected apps (or Claude → Connectors). Send /help here for commands." >/dev/null || true
fi
