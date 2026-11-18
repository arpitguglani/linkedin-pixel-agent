# LinkedIn DMs on your Pixel — $0, nothing on your laptop

Read and reply to LinkedIn messages and InMails from **Gemini** or **Claude** on your phone. You also get:
- **Telegram alerts** within about 5 minutes of a new message, with AI-drafted replies.
- **A daily digest** of every thread where the other person wrote last.

## Install (one command, ~15 minutes, mostly waiting)
1. Create a free Oracle Cloud account at https://signup.cloud.oracle.com. It needs a card for identity
   checks; Always Free resources are never charged. Pick a home region near you.
2. In the Oracle console, click the **Cloud Shell** icon `>_` (top right) and paste:
   ```bash
   bash <(curl -fsSL https://raw.githubusercontent.com/arpitguglani/linkedin-pixel-agent/main/scripts/oci_bootstrap.sh)
   ```
3. Answer the prompts. The script links to each page you need: DuckDNS, @BotFather, AI Studio, and
   Google OAuth client creation. Everything else is automated:
   - Generating every secret.
   - Creating the network, firewall rules and the Always Free Ampere VM. If Oracle reports "out of capacity", it keeps retrying across zones.
   - DNS and HTTPS.
   - Installing and starting the services.
   - Detecting your Telegram chat.
   - Weekly auto-updates.
4. **LinkedIn login:** the script sends a link to your Telegram (`https://login.<you>.duckdns.org/...`).
   Open it on your phone and sign in to LinkedIn.
5. The script then prints two final steps:
   - A **pre-filled Gmail Apps Script**. Paste it at script.google.com, then Run → `install`.
   - The **URL to add** in Gemini (gemini.google.com → Settings → Connected apps → Add custom app) or
     in Claude (Settings → Connectors).

You can re-run the command at any time. It reuses everything that already exists.

## How it works
```
Gmail (LinkedIn notification email)
   └─ Apps Script (your Google account, every 5 min) ──HMAC──┐
                                                             ▼
Pixel: Gemini / Claude ──HTTPS + Google OAuth──► Caddy (Oracle Free VM, DuckDNS)
Pixel: Telegram ◄───────────── watcher :8090 ◄──┤ /webhook
                                auth-proxy :8080 ◄┤ /mcp
                                         └──► linkedin-mcp :8000 (your browser session)
```

### "Awaiting your reply": tracked, not guessed
The watcher keeps a ledger of each conversation's last incoming and last outgoing message. It gets this from three sources:
- **Incoming:** every LinkedIn notification email that the Gmail trigger reports.
- **Outgoing:** every reply you send through Telegram or the assistant.
- **Daily check:** one inbox read reconciles each thread using LinkedIn's own marker. A preview that
  starts with **"You:"** means you sent the last message, so replies you made in the LinkedIn app are picked up too.
  Otherwise the other person wrote last, which also catches messages whose email notification was missed.

A thread is awaiting your reply when its last incoming message is newer than your last outgoing one. Use `/done <id>`
for threads that need no reply.

### Safety
**Nothing is ever sent automatically:**
- **Claude/Gemini:** the proxy refuses `confirm_send=true` unless the exact same reply was first
  previewed as a dry run. The app's own confirmation prompt applies on top.
- **Telegram:** `/send` does a dry run, then shows **✅ Confirm send / ❌ Cancel** buttons. Each
  confirmation sends one message and expires after 15 minutes.
- **Limits:** a 60-second cooldown between sends and a cap of 20 per day.

**Access is locked down:**
- Assistants only see the DM tools, never the profile, jobs, feed or connect tools.
- Only your verified Google account can sign in.
- Every backend listens on localhost only.

**LinkedIn browser activity stays low:**
- One inbox check plus one thread fetch per notification email.
- One inbox check per day.
- Whatever you ask for yourself.

> ⚠️ Automating a personal LinkedIn account through a browser session is against LinkedIn's
> User Agreement. It can trigger checkpoints or restrictions. Keep volumes human-scale.

## Telegram commands
| Command | What it does |
|---|---|
| `/inbox` | Lists recent threads (⏳ they wrote last, ✅ you replied) |
| `/read <id or #>` | Shows the full conversation |
| `/draft <id> [instructions]` | Gives 2–3 AI reply drafts |
| `/send <id> <text>` | Does a dry run, then shows the **Confirm** button |
| `/done <id>` | Marks a thread as not needing a reply |
| `/digest` | Runs the digest now |

## Maintenance
- **Logged out of LinkedIn** (Telegram tells you): `ssh -i ~/.linkedin-agent/id_rsa ubuntu@<domain>`
  from Cloud Shell, then `cd linkedin-pixel-agent && sudo bash scripts/linkedin_login.sh`. The login link is sent to Telegram again.
- **Weekly re-authorization:** while the Google OAuth app is in *Testing* mode, the Gemini/Claude connector needs
  re-authorizing every 7 days. To stop this, publish the app under Google Cloud → Auth → Audience → **Publish**. No verification is needed for the email scope.
- **Logs:** `sudo docker compose --env-file .env -f deploy/compose.yaml logs -f --tail 50`

## Costs
| Item | Cost |
|---|---|
| Oracle A1 VM, DuckDNS, Caddy/Let's Encrypt | \$0 |
| Apps Script, Gemini API free tier, Telegram | \$0 |
| Gemini custom apps, Claude Free plan (1 connector) | \$0 |

## Manual install (any Ubuntu VM)
```bash
git clone https://github.com/arpitguglani/linkedin-pixel-agent && cd linkedin-pixel-agent
cp .env.example .env && nano .env
sudo bash scripts/setup_vm.sh && sudo bash scripts/linkedin_login.sh
```

## Development
```bash
uv sync && uv run pytest && uv run ruff check .
```
