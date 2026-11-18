#!/usr/bin/env bash
# One-time (and re-)login to LinkedIn from your phone or any browser.
#
# 1) From a device with SSH (e.g. Termux on your Pixel, or Oracle Cloud Shell):
#      ssh -L 6080:127.0.0.1:6080 ubuntu@<your-domain>
# 2) On the VM:  bash scripts/linkedin_login.sh
# 3) Open the printed http://localhost:6080/... URL (with token) in the browser
#    on the device that holds the SSH tunnel and sign in to LinkedIn (incl. 2FA).
# The viewer is bound to 127.0.0.1 only and shuts down after login / 30 min.
set -euo pipefail
cd "$(dirname "$0")/.."

docker compose --env-file .env -f deploy/compose.yaml stop linkedin-mcp || true
docker run -it --rm \
  -e TZ="${DAILY_DIGEST_TZ:-America/Los_Angeles}" \
  -v "$PWD/deploy/data/linkedin-profile:/home/pwuser/.linkedin-mcp" \
  -p 127.0.0.1:6080:6080 \
  --shm-size=1g \
  stickerdaniel/linkedin-mcp-server:latest \
  --login --login-viewer
docker compose --env-file .env -f deploy/compose.yaml start linkedin-mcp
echo "LinkedIn session saved. Server restarted."
