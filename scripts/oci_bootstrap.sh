#!/usr/bin/env bash
# =============================================================================
#  One-shot installer — run inside Oracle Cloud Shell (the >_ icon in the OCI
#  console, top right). It is already logged in to your Oracle account.
#
#    bash <(curl -fsSL https://raw.githubusercontent.com/arpitguglani/linkedin-pixel-agent/main/scripts/oci_bootstrap.sh)
#
#  It will:
#    1. ask for the handful of values only you can create (Google OAuth client,
#       DuckDNS, Telegram bot, Gemini key) and generate every secret itself,
#    2. create network + an Always Free Ampere A1 VM (retrying on "out of capacity"),
#    3. install and start everything on the VM,
#    4. send you the LinkedIn login link (open it on your phone),
#    5. print the ready-to-paste Gmail Apps Script and the URL for Gemini/Claude.
#  Re-running is safe: existing resources and answers are reused.
# =============================================================================
set -euo pipefail

REPO="${REPO:-arpitguglani/linkedin-pixel-agent}"
BRANCH="${BRANCH:-main}"
NAME="linkedin-agent"
STATE_DIR="$HOME/.linkedin-agent"
ENV_FILE="$STATE_DIR/.env"
KEY="$STATE_DIR/id_rsa"
SHAPE="VM.Standard.A1.Flex"
OCPUS="${OCPUS:-2}"
MEM_GB="${MEM_GB:-12}"
mkdir -p "$STATE_DIR" && chmod 700 "$STATE_DIR"

bold() { printf '\n\033[1m%s\033[0m\n' "$*"; }
ask() { # ask VAR "Prompt" [default] [secret]
  local var="$1" prompt="$2" def="${3:-}" secret="${4:-}" cur="${!1:-}"
  [[ -n "$cur" ]] && return 0
  local p="$prompt"; [[ -n "$def" ]] && p="$prompt [$def]"
  if [[ -n "$secret" ]]; then read -r -s -p "$p: " val; echo; else read -r -p "$p: " val; fi
  val="${val:-$def}"
  [[ -z "$val" ]] && { echo "  (required)"; ask "$@"; return; }
  printf -v "$var" '%s' "$val"
}
save_env() { # KEY VALUE
  touch "$ENV_FILE"; chmod 600 "$ENV_FILE"
  grep -v "^$1=" "$ENV_FILE" >"$ENV_FILE.tmp" || true
  printf '%s=%s\n' "$1" "$2" >>"$ENV_FILE.tmp" && mv "$ENV_FILE.tmp" "$ENV_FILE"
}
rand() { python3 -c "import secrets;print(secrets.token_urlsafe(48))"; }
fernet() { python3 -c "import base64,os;print(base64.urlsafe_b64encode(os.urandom(32)).decode())"; }

# shellcheck disable=SC1090
[[ -f "$ENV_FILE" ]] && { set -a; source "$ENV_FILE"; set +a; }

# --------------------------------------------------------------------------
bold "Step 1/5 — your accounts (values are saved in $ENV_FILE)"
cat <<'TXT'
You need (each takes ~1 minute):
  a) Telegram: talk to @BotFather -> /newbot -> copy the bot token
  b) https://aistudio.google.com/apikey -> Create API key   (optional, for AI drafts)
  The server gets a free hostname <ip>.sslip.io automatically (no account needed).
TXT
ask ALLOWED_EMAIL "Your Google account email (the only one allowed in)"
ask TELEGRAM_BOT_TOKEN "Telegram bot token" "" secret
if [[ -z "${GEMINI_API_KEY+x}" ]]; then read -r -s -p "Gemini API key (Enter to skip): " GEMINI_API_KEY; echo; fi
ask DAILY_DIGEST_HOUR "Daily digest hour (0-23, local)" "18"
ask DAILY_DIGEST_TZ "Your timezone" "America/Los_Angeles"

JWT_SIGNING_KEY="${JWT_SIGNING_KEY:-$(rand)}"
STORAGE_ENCRYPTION_KEY="${STORAGE_ENCRYPTION_KEY:-$(fernet)}"
WEBHOOK_SECRET="${WEBHOOK_SECRET:-$(rand)}"
for k in ALLOWED_EMAIL TELEGRAM_BOT_TOKEN GEMINI_API_KEY DAILY_DIGEST_HOUR DAILY_DIGEST_TZ \
  JWT_SIGNING_KEY STORAGE_ENCRYPTION_KEY WEBHOOK_SECRET; do save_env "$k" "${!k}"; done

# Telegram chat id: auto-detect after you press Start in the bot chat
if [[ -z "${TELEGRAM_CHAT_ID:-}" ]]; then
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
    --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" --data-urlencode "text=✅ Connected. Setting up your LinkedIn agent…" >/dev/null
  echo "  Telegram connected (chat ${TELEGRAM_CHAT_ID})."
fi
save_env DAILY_DIGEST_TZ "$DAILY_DIGEST_TZ"

# --------------------------------------------------------------------------
bold "Step 2/5 — Oracle network + Always Free VM"
OCI_TENANCY="${OCI_TENANCY:-$(awk -F= '/^tenancy/{gsub(/ /,"",$2);print $2;exit}' "${OCI_CLI_CONFIG_FILE:-$HOME/.oci/config}" 2>/dev/null)}"
C="${OCI_COMPARTMENT:-${OCI_TENANCY:?Run this in OCI Cloud Shell or configure ~/.oci/config}}"
q() { oci "$@" 2>/dev/null; }

VCN="$(q network vcn list -c "$C" --display-name "$NAME-vcn" --query 'data[0].id' --raw-output || true)"
if [[ -z "$VCN" || "$VCN" == "null" ]]; then
  VCN="$(oci network vcn create -c "$C" --cidr-blocks '["10.0.0.0/16"]' --display-name "$NAME-vcn" \
    --dns-label liagent --wait-for-state AVAILABLE --query data.id --raw-output)"
fi
IGW="$(q network internet-gateway list -c "$C" --vcn-id "$VCN" --query 'data[0].id' --raw-output || true)"
if [[ -z "$IGW" || "$IGW" == "null" ]]; then
  IGW="$(oci network internet-gateway create -c "$C" --vcn-id "$VCN" --is-enabled true \
    --display-name "$NAME-igw" --wait-for-state AVAILABLE --query data.id --raw-output)"
fi
RT="$(oci network vcn get --vcn-id "$VCN" --query 'data."default-route-table-id"' --raw-output)"
oci network route-table update --rt-id "$RT" --force --route-rules \
  "[{\"destination\":\"0.0.0.0/0\",\"destinationType\":\"CIDR_BLOCK\",\"networkEntityId\":\"$IGW\"}]" >/dev/null
SL="$(oci network vcn get --vcn-id "$VCN" --query 'data."default-security-list-id"' --raw-output)"
rule() { echo "{\"source\":\"0.0.0.0/0\",\"protocol\":\"6\",\"isStateless\":false,\"tcpOptions\":{\"destinationPortRange\":{\"min\":$1,\"max\":$1}}}"; }
oci network security-list update --security-list-id "$SL" --force \
  --ingress-security-rules "[$(rule 22),$(rule 80),$(rule 443)]" \
  --egress-security-rules '[{"destination":"0.0.0.0/0","protocol":"all","isStateless":false}]' >/dev/null
SUBNET="$(q network subnet list -c "$C" --vcn-id "$VCN" --query 'data[0].id' --raw-output || true)"
if [[ -z "$SUBNET" || "$SUBNET" == "null" ]]; then
  SUBNET="$(oci network subnet create -c "$C" --vcn-id "$VCN" --cidr-block 10.0.0.0/24 \
    --display-name "$NAME-subnet" --dns-label pub --wait-for-state AVAILABLE --query data.id --raw-output)"
fi
echo "  Network ready."

[[ -f "$KEY" ]] || ssh-keygen -q -t rsa -b 4096 -N "" -f "$KEY"

INSTANCE="$(q compute instance list -c "$C" --display-name "$NAME" --lifecycle-state RUNNING \
  --query 'data[0].id' --raw-output || true)"
if [[ -z "$INSTANCE" || "$INSTANCE" == "null" ]]; then
  IMG="$(oci compute image list -c "$C" --operating-system "Canonical Ubuntu" --operating-system-version "24.04" \
    --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC --limit 1 --query 'data[0].id' --raw-output)"
  CLOUD_INIT="$STATE_DIR/cloud-init.sh"
  cat >"$CLOUD_INIT" <<EOF
#!/bin/bash
apt-get update -q && apt-get install -y -q git curl jq
curl -fsSL https://get.docker.com | sh
usermod -aG docker ubuntu
sudo -u ubuntu git clone -q -b ${BRANCH} https://github.com/${REPO}.git /home/ubuntu/linkedin-pixel-agent
touch /var/lib/cloud/instance/linkedin-agent-ready
EOF
  mapfile -t ADS < <(oci iam availability-domain list -c "$C" --query 'data[].name' --raw-output | jq -r '.[]')
  attempt=0
  while [[ -z "${INSTANCE:-}" || "$INSTANCE" == "null" ]]; do
    for AD in "${ADS[@]}"; do
      attempt=$((attempt + 1))
      echo "  Launching A1 VM in $AD (attempt $attempt)..."
      if out="$(oci compute instance launch -c "$C" --availability-domain "$AD" --shape "$SHAPE" \
          --shape-config "{\"ocpus\":$OCPUS,\"memoryInGBs\":$MEM_GB}" --image-id "$IMG" --subnet-id "$SUBNET" \
          --assign-public-ip true --display-name "$NAME" --boot-volume-size-in-gbs 50 \
          --ssh-authorized-keys-file "$KEY.pub" --user-data-file "$CLOUD_INIT" \
          --instance-options '{"areLegacyImdsEndpointsDisabled":true}' \
          --wait-for-state RUNNING --query data.id --raw-output 2>&1)"; then
        INSTANCE="$(echo "$out" | grep -oE 'ocid1\.instance[^"[:space:]]+' | head -1)"
        break
      elif grep -qi "capacity" <<<"$out"; then
        echo "    Out of capacity there."
      else
        echo "$out" >&2; exit 1
      fi
    done
    [[ -n "${INSTANCE:-}" ]] || { echo "  All ADs full; retrying in 60s (Ctrl+C to stop, re-run later)..."; sleep 60; }
  done
fi
IP="$(oci compute instance list-vnics --instance-id "$INSTANCE" --query 'data[0]."public-ip"' --raw-output)"
echo "  VM running at $IP"
if [[ -n "${DUCKDNS_TOKEN:-}" && "${MCP_DOMAIN:-}" == *.duckdns.org ]]; then
  curl -fsS "https://www.duckdns.org/update?domains=${MCP_DOMAIN%.duckdns.org}&token=${DUCKDNS_TOKEN}&ip=${IP}" >/dev/null
else
  MCP_DOMAIN="${IP//./-}.sslip.io"
fi
save_env MCP_DOMAIN "$MCP_DOMAIN"
echo "  Hostname: ${MCP_DOMAIN} -> ${IP}"

bold "Google sign-in client (lets Gemini/Claude sign in as you)"
if [[ -z "${GOOGLE_CLIENT_ID:-}" ]]; then
cat <<TXT
  1. https://console.cloud.google.com/projectcreate  -> create a project
  2. https://console.cloud.google.com/auth/branding  -> Get started: app name "LinkedIn agent",
     your email, Audience = External -> Create
  3. https://console.cloud.google.com/auth/audience  -> Test users -> Add ${ALLOWED_EMAIL}
  4. https://console.cloud.google.com/auth/clients/create  -> Web application
     Authorized redirect URI:  https://${MCP_DOMAIN}/auth/callback
     -> Create, copy Client ID + Client secret
TXT
fi
ask GOOGLE_CLIENT_ID "Google OAuth Client ID"
ask GOOGLE_CLIENT_SECRET "Google OAuth Client secret" "" secret
save_env GOOGLE_CLIENT_ID "$GOOGLE_CLIENT_ID"; save_env GOOGLE_CLIENT_SECRET "$GOOGLE_CLIENT_SECRET"

# --------------------------------------------------------------------------
bold "Step 3/5 — installing on the VM (3-6 minutes)"
SSH=(ssh -i "$KEY" -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10 "ubuntu@$IP")
for _ in $(seq 1 60); do "${SSH[@]}" true 2>/dev/null && break; sleep 5; done
for _ in $(seq 1 120); do
  "${SSH[@]}" test -f /var/lib/cloud/instance/linkedin-agent-ready 2>/dev/null && break; sleep 5
done
"${SSH[@]}" "cd ~/linkedin-pixel-agent && git pull -q --ff-only || true"
scp -q -i "$KEY" "$ENV_FILE" "ubuntu@$IP:linkedin-pixel-agent/.env"
"${SSH[@]}" "cd ~/linkedin-pixel-agent && sudo bash scripts/setup_vm.sh" | tail -n 3
for _ in $(seq 1 60); do
  curl -fsS "https://${MCP_DOMAIN}/webhook/health" >/dev/null 2>&1 && break; sleep 5
done
curl -fsS "https://${MCP_DOMAIN}/webhook/health" >/dev/null && echo "  HTTPS is live: https://${MCP_DOMAIN}" ||
  echo "  (HTTPS not reachable yet — certificate may take another minute)"

# --------------------------------------------------------------------------
bold "Step 4/5 — LinkedIn login (link also sent to your Telegram)"
if "${SSH[@]}" "sudo test -d ~/linkedin-pixel-agent/deploy/data/linkedin-profile/profile"; then
  echo "  Existing LinkedIn session found; skipping. (Re-login: ssh in and run scripts/linkedin_login.sh)"
else
  "${SSH[@]}" -t "cd ~/linkedin-pixel-agent && sudo bash scripts/linkedin_login.sh"
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
   (Also saved at $GS — Cloud Shell menu -> Download)

B) Phone assistant:
   Gemini: gemini.google.com (Desktop site) -> Settings -> Connected apps -> Add custom app
           URL: https://${MCP_DOMAIN}/mcp   then sign in as ${ALLOWED_EMAIL}
   Claude: claude.ai -> Settings -> Connectors -> Add custom connector
           URL: https://${MCP_DOMAIN}/mcp

Done! Telegram will ping you on new LinkedIn messages and send a digest daily at ${DAILY_DIGEST_HOUR}:00.
SSH later with: ssh -i $KEY ubuntu@${MCP_DOMAIN}
TXT
curl -fsS "https://api.telegram.org/bot${TELEGRAM_BOT_TOKEN}/sendMessage" \
  --data-urlencode "chat_id=${TELEGRAM_CHAT_ID}" \
  --data-urlencode "text=🎉 Server is up. Add https://${MCP_DOMAIN}/mcp in Gemini → Connected apps (or Claude → Connectors). Send /help here for commands." >/dev/null || true
