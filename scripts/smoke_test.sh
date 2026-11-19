#!/usr/bin/env bash
# Cleans any stale Chromium Singleton* locks and runs a non-destructive
# smoke test across all 5 exposed LinkedIn MCP tools (no messages are sent).
set -euo pipefail
cd "$(dirname "$0")/.."
DC=(docker compose --env-file .env -f deploy/compose.yaml)

locks="$(find "$PWD/deploy/data/linkedin-profile" -name 'Singleton*' 2>/dev/null || true)"
if [[ -n "$locks" ]]; then
  echo "Found stale Chromium lock files; cleaning and restarting linkedin-mcp..."
  "${DC[@]}" stop linkedin-mcp >/dev/null
  find "$PWD/deploy/data/linkedin-profile" -name 'Singleton*' -delete
  "${DC[@]}" up -d linkedin-mcp >/dev/null
  sleep 3
fi

docker exec -i linkedin-pixel-agent-watcher-1 python - <<'PY'
import asyncio, json, sys
from watcher.linkedin import LinkedInMCP, extract_threads, section_text

async def main() -> int:
    li = LinkedInMCP("http://127.0.0.1:8000/mcp")
    failures = 0

    async def run(name, args=None):
        nonlocal failures
        try:
            res = await li.call(name, args or {})
            print(f"[PASS] {name}")
            return res
        except Exception as exc:
            failures += 1
            print(f"[FAIL] {name}: {exc}")
            return None

    status = await run("get_session_status")
    if status:
        print("       session:", json.dumps(status)[:200])

    inbox = await run("get_inbox", {"limit": 5})
    threads = extract_threads(inbox) if inbox else []
    print(f"       inbox threads discovered: {len(threads)}")
    if inbox and isinstance(inbox, dict) and inbox.get("section_errors"):
        print("       section_errors:", inbox.get("section_errors"))

    if not threads:
        print("[SKIP] search_conversations / get_conversation / reply_to_conversation (no thread_id in inbox)")
        return 1 if failures else 0

    sample = threads[0]
    kw = (sample.label.split()[0] if sample.label else "hello")
    await run("search_conversations", {"keywords": kw})

    conv = await run("get_conversation", {"thread_id": sample.thread_id})
    if conv:
        text = section_text(conv)
        print(f"       conversation chars: {len(text)}")

    dry = await run(
        "reply_to_conversation",
        {"thread_id": sample.thread_id, "message": "dry run verification", "confirm_send": False},
    )
    if dry and isinstance(dry, dict):
        print(f"       reply dry-run status={dry.get('status')} sent={dry.get('sent')}")

    return 1 if failures else 0

sys.exit(asyncio.run(main()))
PY
