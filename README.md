# LinkedIn DMs on your Pixel — $0, nothing on your laptop

Read and reply to LinkedIn messages/InMails from **Gemini** or **Claude** on your phone,
get **Telegram alerts within ~5 minutes** of a new message (with AI-drafted replies), and a
**once-a-day digest** of unread / awaiting-reply threads.

```
Gmail (LinkedIn notification email)
   └─ Apps Script (your Google account, every 5 min) ──HMAC──┐
                                                             ▼
Pixel: Gemini / Claude ──HTTPS+Google OAuth──► Caddy (Oracle Free VM, DuckDNS)
Pixel: Telegram ◄──────────────────────────────┐  ├─ /mcp      → auth-proxy :8080 ─┐
                                               │  └─ /webhook  → watcher    :8090 ─┤
                                               └──────────── watcher ◄─────────────┤
                                                       linkedin-mcp :8000 (browser session)
```

**Safety:** nothing is ever sent automatically.
- **Claude/Gemini:** the proxy refuses `confirm_send=true` unless the exact same reply was first
  previewed as a dry run (`confirm_send=false`). The app's own write confirmation applies on top.
- **Telegram:** `/send` does a dry run, then shows **✅ Confirm send / ❌ Cancel** buttons. Each
  confirmation sends one message and expires after 15 minutes.
- **Limits:** a 60 s cooldown and 20 sends/day by default.
- **Exposed tools:** only the DM tools. The upstream server's profile, jobs, feed and connect tools are hidden.
- **Access:** only your Google account (verified email) is let in. Every backend listens on localhost only.

**LinkedIn browser activity is minimal:**
- One inbox check plus one thread fetch per notification email.
- One inbox check per day for the digest.
- Anything you ask for yourself.

There is no constant polling.

> ⚠️ Automating a personal LinkedIn account through a browser session is against LinkedIn's
> User Agreement. It can trigger security checkpoints or restrictions. You accepted this risk. Keep volumes human-scale.

---

## 0. What you need (all free)
- A personal Google account (Gemini custom apps: US, 18+, English, Keep Activity on).
- A card for Oracle Cloud identity verification. Always Free resources are never charged.
- Telegram on your Pixel.
- Termux on your Pixel (only for the one-time LinkedIn login over SSH). Oracle **Cloud Shell**
  in the browser works as well.

## 1. Oracle Cloud Always Free VM
1. Sign up at https://signup.cloud.oracle.com. Choose a home region with Ampere capacity; you cannot change it later.
2. Go to **Compute → Instances → Create**:
   - Image: **Ubuntu 24.04**.
   - Shape: **VM.Standard.A1.Flex**, 2 OCPU / 12 GB. The Always Free allowance is 4 OCPU / 24 GB in total.
   - Upload or generate an SSH key.

   If you get *"Out of capacity"*, retry later or try a different availability domain.
3. Go to **Networking → VCN → Security List → Add Ingress Rules**: TCP **80** and **443** from `0.0.0.0/0`.
4. Get a free subdomain at https://www.duckdns.org (sign in with Google). Create `yourname`,
   point it at the VM's public IP, and copy the **token**.

## 2. Google OAuth client (for Gemini/Claude login)
1. Go to https://console.cloud.google.com and create a project.
2. Open **APIs & Services → OAuth consent screen** and set it up:
   - User type **External**, publishing status **Testing**.
   - Add **your Gmail as a Test user**.
   - Scopes: `openid` and `.../auth/userinfo.email`.
3. Open **Credentials → Create credentials → OAuth client ID** and create a **Web application** client:
   - Authorized redirect URI: `https://yourname.duckdns.org/auth/callback`
   - Copy the Client ID and Client secret.

> In Testing mode Google refresh tokens expire after 7 days, so expect to re-authorize the connector about once a week.
> You can publish the app to avoid this. With only the basic `email` scope, no verification is needed.

## 3. Telegram bot + Gemini API key
1. In Telegram, message **@BotFather** with `/newbot` and copy the token.
2. Send your bot any message. Then open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy `chat.id`.
3. Create a Gemini API key at https://aistudio.google.com/apikey. The free tier is plenty.

## 4. Deploy on the VM
```bash
ssh ubuntu@yourname.duckdns.org
git clone <this repo> linkedin-pixel-agent && cd linkedin-pixel-agent
cp .env.example .env && nano .env      # fill everything; generate secrets as shown inside
bash scripts/setup_vm.sh               # docker, firewall, swap, DuckDNS cron
exit                                   # re-login so the docker group applies
```

## 5. One-time LinkedIn login (from your phone)
1. On the Pixel, open Termux and run:
   ```bash
   pkg install openssh
   ssh -L 6080:127.0.0.1:6080 ubuntu@yourname.duckdns.org
   cd linkedin-pixel-agent && bash scripts/linkedin_login.sh
   ```
2. Open the printed `http://localhost:6080/...` URL in Chrome on the Pixel.
3. Sign in to LinkedIn, including 2FA. The session is saved to `deploy/data/linkedin-profile`.

Then start everything:
```bash
docker compose --env-file .env -f deploy/compose.yaml up -d --build
docker compose --env-file .env -f deploy/compose.yaml logs -f --tail 50
curl https://yourname.duckdns.org/webhook/health     # {"status":"ok"}
```
Repeat step 5 whenever Telegram says the session is logged out.

## 6. Gmail trigger (Apps Script)
1. Go to https://script.google.com and create a **New project**. Paste [`gmail_trigger/Code.gs`](gmail_trigger/Code.gs).
2. Under **Project Settings → Script properties**, add:
   - `WEBHOOK_URL` = `https://yourname.duckdns.org/webhook/gmail`
   - `WEBHOOK_SECRET` = the same value as in `.env`
3. Select the `install` function and click **Run**. Approve the Gmail and external-request permissions.
4. Make sure LinkedIn email notifications for **messages and InMails** are on: LinkedIn → Settings → Notifications → Email.

## 7. Connect your phone assistants
**Gemini (recommended on Pixel):**
1. On **gemini.google.com**, use a desktop browser or "Desktop site" in Pixel Chrome.
2. Go to **Settings → Connected apps → Add custom app** and enter the URL `https://yourname.duckdns.org/mcp`.
3. Sign in with your allowlisted Google account and approve the consent page.
4. The app now shows up in the Gemini Android app.

**Claude (Free plan allows 1 custom connector):**
1. On claude.ai, go to **Settings → Connectors → Add custom connector** and enter `https://yourname.duckdns.org/mcp`.
2. Connect and sign in with Google.
3. Set the reply tool to *"Ask before use"*. The connector then works in the Claude Android app.

Example prompts:
- "What LinkedIn messages need a reply?"
- "Open my thread with Jane and draft a friendly yes."
- "Send it."

When you say "send it", the assistant first previews the reply, then asks you to confirm.

## Telegram commands
| Command | What it does |
|---|---|
| `/inbox` | Lists recent threads with numbers and thread ids. |
| `/read <id or #>` | Shows the full conversation. |
| `/draft <id> [instructions]` | Gives 2–3 AI reply drafts. |
| `/send <id> <text>` | Does a dry run, then shows a **Confirm** button. |
| `/digest` | Runs the daily digest now. |

## Costs
| Item | Cost |
|---|---|
| Oracle A1 VM, DuckDNS, Caddy/Let's Encrypt | \$0 |
| Apps Script, Gemini API free tier, Telegram | \$0 |
| Gemini app custom apps, Claude Free connector | \$0 |

## Troubleshooting
- **Connector fails to add:** check the following:
  - `https://<domain>/.well-known/oauth-authorization-server` returns JSON.
  - The redirect URI in Google Cloud matches exactly.
  - Your Gmail is listed as a test user.
- **"Authorization failed … insufficient permissions":** you signed in with a different Google account than `ALLOWED_EMAIL`.
- **"Send blocked: first call this tool with confirm_send=false…":** this is intended. Ask the assistant to preview first.
- **No alerts:**
  - Check the Apps Script **Executions** log.
  - Check the `docker compose logs watcher` output.
  - Make sure LinkedIn notification emails are reaching Gmail.

## Development
```bash
uv sync && uv run pytest && uv run ruff check .
```
