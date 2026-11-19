"""Watcher service: Gmail-triggered fetches, daily digest, Telegram control.

Runs on 127.0.0.1:8090 behind Caddy (only /webhook/* is routed here).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import random
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response

from auth_proxy.policy import RateLimitExceeded, WriteRateLimiter
from watcher import security
from watcher.ai import Drafter
from watcher.digest import LedgerEntry, format_digest, parse_inbox
from watcher.linkedin import (
    LinkedInMCP,
    LinkedInSessionError,
    extract_threads,
    find_thread_for_sender,
    section_text,
)
from watcher.store import Store
from watcher.telegram import Telegram

log = logging.getLogger("watcher")

RELOGIN_HINT = (
    "⚠️ LinkedIn session looks logged out. Send /login and I'll message you a "
    "secure login link to open on your phone."
)


@dataclass
class Settings:
    webhook_secret: str
    linkedin_mcp_url: str = "http://127.0.0.1:8000/mcp"
    db_path: str = "/data/watcher.db"
    login_request_file: str = "/data/login.request"
    telegram_token: str | None = None
    telegram_chat_id: str | None = None
    gemini_api_key: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    digest_hour: int = 18
    digest_tz: str = "America/Los_Angeles"
    jitter_seconds: tuple[int, int] = (10, 30)
    write_cooldown_seconds: float = 60
    write_daily_cap: int = 20

    @classmethod
    def from_env(cls) -> Settings:
        e = os.environ
        return cls(
            webhook_secret=e["WEBHOOK_SECRET"],
            linkedin_mcp_url=e.get("LINKEDIN_MCP_URL", cls.linkedin_mcp_url),
            db_path=e.get("WATCHER_DB", cls.db_path),
            login_request_file=e.get("LOGIN_REQUEST_FILE", cls.login_request_file),
            telegram_token=e.get("TELEGRAM_BOT_TOKEN") or None,
            telegram_chat_id=e.get("TELEGRAM_CHAT_ID") or None,
            gemini_api_key=e.get("GEMINI_API_KEY") or None,
            gemini_model=e.get("GEMINI_MODEL", cls.gemini_model),
            digest_hour=int(e.get("DAILY_DIGEST_HOUR", cls.digest_hour)),
            digest_tz=e.get("DAILY_DIGEST_TZ", cls.digest_tz),
            write_cooldown_seconds=float(e.get("WRITE_COOLDOWN_SECONDS", 60)),
            write_daily_cap=int(e.get("WRITE_DAILY_CAP", 20)),
        )


def _parse_ts(value: Any) -> float:
    if isinstance(value, str) and value:
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
        except ValueError:
            pass
    return time.time()


def digest_target(day: date, hour: int, tz: ZoneInfo, jitter_minutes: int = 15) -> datetime:
    """Deterministic per-day jittered digest time (looks less bot-like)."""
    offset = random.Random(day.isoformat()).randint(-jitter_minutes, jitter_minutes)
    return datetime(day.year, day.month, day.day, hour, tzinfo=tz) + timedelta(minutes=offset)


def digest_due(now: datetime, hour: int, last_run: str | None, jitter_minutes: int = 15) -> bool:
    today = now.date()
    if last_run == today.isoformat():
        return False
    return now >= digest_target(today, hour, now.tzinfo, jitter_minutes)  # type: ignore[arg-type]


class Agent:
    """All business logic; FastAPI/Telegram are thin shells around it."""

    def __init__(
        self,
        settings: Settings,
        store: Store,
        linkedin: LinkedInMCP,
        telegram: Telegram,
        drafter: Drafter,
        sleep=asyncio.sleep,
    ) -> None:
        self.s = settings
        self.store = store
        self.li = linkedin
        self.tg = telegram
        self.ai = drafter
        self.sleep = sleep
        self.lock = asyncio.Lock()  # one LinkedIn browser action at a time
        self.limiter = WriteRateLimiter(settings.write_cooldown_seconds, settings.write_daily_cap)
        self.last_threads: list[str] = []

    # ---------- Gmail webhook ----------
    async def handle_email_event(self, event: dict[str, Any]) -> None:
        sender = str(event.get("from_name") or "").strip()
        snippet = str(event.get("snippet") or "").strip()
        subject = str(event.get("subject") or "").strip()
        received = _parse_ts(event.get("received_at"))
        await self.store.record_incoming(sender, received, snippet[:200])
        lo, hi = self.s.jitter_seconds
        await self.sleep(random.uniform(lo, hi))
        header = f"💬 New LinkedIn message from {sender or 'someone'}"
        try:
            async with self.lock:
                inbox = await self.li.inbox(limit=10)
                thread = find_thread_for_sender(extract_threads(inbox), sender)
                convo_text = ""
                if thread:
                    await self.store.record_incoming(sender, received, "", thread.thread_id)
                    convo_text = section_text(await self.li.conversation(thread.thread_id))
        except LinkedInSessionError:
            await self.tg.send(f"{header}\n\n{subject}\n{snippet}\n\n{RELOGIN_HINT}")
            return
        except Exception as exc:
            log.warning("fetch failed: %s", type(exc).__name__)
            note = f"(Couldn't open LinkedIn: {type(exc).__name__})"
            await self.tg.send(f"{header}\n\n{subject}\n{snippet}\n\n{note}")
            return

        basis = convo_text or f"{sender}: {snippet}"
        triage = await self.ai.triage(basis)
        lines = [header]
        if thread:
            lines.append(f"Thread: {thread.thread_id}")
        lines.append("")
        # Without a Gemini key: show the latest part of the conversation itself.
        lines.append(triage or (convo_text[-1200:] if convo_text else "") or snippet or "(no preview)")
        if not triage:
            lines += [
                "",
                "Tip: open Gemini/Claude and say “draft a reply to my LinkedIn thread with "
                f"{sender or 'them'}”.",
            ]
        if thread:
            lines += [
                "",
                f"Reply: /send {thread.thread_id} <your text>",
                f"Redraft: /draft {thread.thread_id} <instructions>",
                f"No reply needed: /done {thread.thread_id}",
            ]
        await self.tg.send("\n".join(lines))

    # ---------- Daily digest ----------
    async def run_digest(self) -> str | None:
        try:
            async with self.lock:
                inbox = await self.li.inbox(limit=20)
        except LinkedInSessionError:
            await self.tg.send(RELOGIN_HINT)
            return None
        now = time.time()
        for row in self._rows(inbox):
            await self.store.reconcile(row.name, row.you_sent_last, row.preview, row.thread_id, now)
        since = float(await self.store.get("last_digest_ts") or 0)
        await self.store.set("last_digest_ts", str(now))
        awaiting = [LedgerEntry(*r) for r in await self.store.awaiting()]
        text = format_digest(awaiting, await self.store.replied_since(since) if since else 0, now)
        if text:
            await self.tg.send(text)
        return text

    @staticmethod
    def _rows(inbox: Any):
        labelled = [(t.label, t.thread_id) for t in extract_threads(inbox) if t.label]
        return parse_inbox(section_text(inbox), labelled)

    async def digest_loop(self) -> None:
        if not self.tg.enabled:
            return
        tz = ZoneInfo(self.s.digest_tz)
        while True:
            try:
                now = datetime.now(tz)
                if digest_due(now, self.s.digest_hour, await self.store.get("last_digest")):
                    await self.store.set("last_digest", now.date().isoformat())
                    await self.run_digest()
            except Exception as exc:
                log.warning("digest loop error: %s", type(exc).__name__)
            await asyncio.sleep(60)

    # ---------- Telegram commands ----------
    def _authorized(self, chat_id: Any) -> bool:
        return self.tg.chat_id is not None and str(chat_id) == self.tg.chat_id

    async def handle_update(self, update: dict[str, Any]) -> None:
        if cb := update.get("callback_query"):
            chat_id = (cb.get("message") or {}).get("chat", {}).get("id")
            if not self._authorized(chat_id):
                return
            await self.handle_callback(cb.get("id", ""), cb.get("data", ""))
            return
        msg = update.get("message") or {}
        if not self._authorized(msg.get("chat", {}).get("id")):
            return  # silently ignore strangers
        await self.handle_command((msg.get("text") or "").strip())

    async def handle_command(self, text: str) -> None:
        cmd, _, rest = text.partition(" ")
        cmd = cmd.split("@")[0].lower()
        try:
            if cmd in ("/start", "/help"):
                await self.tg.send(
                    "Commands:\n/inbox – recent threads\n/read <id> – open a thread\n"
                    "/draft <id> [instructions] – AI reply drafts\n"
                    "/send <id> <text> – reply (asks for confirmation)\n"
                    "/done <id> – mark as not needing a reply\n/digest – run digest now\n"
                    "/login – re-login to LinkedIn"
                )
            elif cmd == "/inbox":
                await self.cmd_inbox()
            elif cmd == "/read":
                await self.cmd_read(rest.strip())
            elif cmd == "/draft":
                tid, _, instr = rest.strip().partition(" ")
                await self.cmd_draft(tid, instr)
            elif cmd == "/send":
                tid, _, body = rest.strip().partition(" ")
                await self.cmd_send(tid, body)
            elif cmd == "/login":
                # The host's cron picks this up within a minute and runs
                # scripts/linkedin_login.sh, which sends the link here.
                with open(self.s.login_request_file, "w") as fh:
                    fh.write(str(time.time()))
                await self.tg.send("🔐 Starting LinkedIn login… the link arrives here in ~1–2 minutes.")
            elif cmd == "/done":
                tid = self._resolve(rest.strip())
                await self.store.record_outgoing(time.time(), thread_id=tid)
                await self.tg.send(f"Marked {tid} as handled.")
            elif cmd == "/digest":
                if not await self.run_digest():
                    await self.tg.send("Nothing awaiting your reply. 🎉")
        except LinkedInSessionError:
            await self.tg.send(RELOGIN_HINT)

    async def cmd_inbox(self) -> None:
        async with self.lock:
            inbox = await self.li.inbox(limit=15)
        threads = extract_threads(inbox)
        self.last_threads = [t.thread_id for t in threads]
        out = ["📥 Inbox (⏳ = they wrote last, ✅ = you replied)"]
        for r in self._rows(inbox)[:15]:
            out.append(f"{'✅' if r.you_sent_last else '⏳'} {r.name}: {r.preview[:80]}")
        if threads:
            out.append("\nThread ids:")
            out += [f"{i + 1}. {t.label or '?'} → {t.thread_id}" for i, t in enumerate(threads[:15])]
        await self.tg.send("\n".join(out))

    def _resolve(self, tid: str) -> str:
        if tid.isdigit() and 0 < int(tid) <= len(self.last_threads):
            return self.last_threads[int(tid) - 1]
        return tid

    async def cmd_read(self, tid: str) -> None:
        if not tid:
            await self.tg.send("Usage: /read <thread id or # from /inbox>")
            return
        tid = self._resolve(tid)
        async with self.lock:
            convo = await self.li.conversation(tid)
        await self.tg.send(section_text(convo)[-3500:] or "(empty)")

    async def cmd_draft(self, tid: str, instructions: str) -> None:
        if not tid:
            await self.tg.send("Usage: /draft <thread id> [instructions]")
            return
        tid = self._resolve(tid)
        async with self.lock:
            convo = await self.li.conversation(tid)
        drafts = await self.ai.triage(section_text(convo), instructions)
        await self.tg.send((drafts or "(AI unavailable)") + f"\n\nSend with: /send {tid} <text>")

    async def cmd_send(self, tid: str, body: str) -> None:
        body = body.strip()
        if not tid or not body:
            await self.tg.send("Usage: /send <thread id> <message text>")
            return
        tid = self._resolve(tid)
        async with self.lock:
            preview = await self.li.reply(tid, body, confirm=False)  # dry run: verifies recipient
        who = (preview or {}).get("participant") if isinstance(preview, dict) else None
        token = await self.store.create_pending_send(tid, body)
        await self.tg.send(
            f"About to reply to {who or 'thread ' + tid}:\n\n{body}\n\nConfirm?",
            buttons=[
                [
                    {"text": "✅ Confirm send", "callback_data": f"send:{token}"},
                    {"text": "❌ Cancel", "callback_data": f"cancel:{token}"},
                ]
            ],
        )

    async def handle_callback(self, callback_id: str, data: str) -> None:
        action, _, token = data.partition(":")
        pending = await self.store.pop_pending_send(token)
        if action == "cancel" or pending is None:
            await self.tg.answer_callback(callback_id, "Cancelled" if action == "cancel" else "Expired")
            if action != "cancel":
                await self.tg.send("That confirmation expired or was already used.")
            return
        if action != "send":
            return
        tid, body = pending
        try:
            self.limiter.check_and_record()
        except RateLimitExceeded as exc:
            await self.tg.answer_callback(callback_id, "Rate limited")
            await self.tg.send(str(exc))
            return
        await self.tg.answer_callback(callback_id, "Sending…")
        async with self.lock:
            result = await self.li.reply(tid, body, confirm=True)
        status = result.get("status") if isinstance(result, dict) else None
        sent = result.get("sent") if isinstance(result, dict) else None
        if sent:
            who = result.get("participant") if isinstance(result, dict) else None
            await self.store.record_outgoing(time.time(), name=who, thread_id=tid)
        await self.tg.send("✅ Sent." if sent else f"⚠️ Not confirmed as sent (status: {status}).")

    async def telegram_loop(self) -> None:
        if not self.tg.enabled:
            return
        offset = await self.store.get("tg_offset")
        next_offset = int(offset) if offset else None
        while True:
            try:
                for upd in await self.tg.get_updates(next_offset):
                    next_offset = int(upd["update_id"]) + 1
                    await self.store.set("tg_offset", str(next_offset))
                    asyncio.create_task(self._safe(self.handle_update(upd)))
            except Exception as exc:
                log.warning("telegram poll error: %s", type(exc).__name__)
                await asyncio.sleep(5)

    async def _safe(self, coro) -> None:
        try:
            await coro
        except Exception as exc:
            log.warning("handler error: %s", type(exc).__name__)
            with contextlib.suppress(Exception):
                await self.tg.send(f"Error: {type(exc).__name__}")


def create_app(agent: Agent | None = None, start_background: bool = True) -> FastAPI:
    if agent is None:
        s = Settings.from_env()
        agent = Agent(
            s,
            Store(s.db_path),
            LinkedInMCP(s.linkedin_mcp_url),
            Telegram(s.telegram_token, s.telegram_chat_id),
            Drafter(s.gemini_api_key, s.gemini_model),
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await agent.store.init()
        tasks = []
        if start_background:
            tasks = [asyncio.create_task(agent.telegram_loop()), asyncio.create_task(agent.digest_loop())]
        yield
        for t in tasks:
            t.cancel()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.agent = agent
    background: set[asyncio.Task] = set()

    @app.post("/webhook/gmail")
    async def gmail_webhook(request: Request) -> Response:
        body = await request.body()
        if not security.verify(
            agent.s.webhook_secret,
            body,
            request.headers.get("x-timestamp"),
            request.headers.get("x-signature-256"),
        ):
            return Response(status_code=401)
        try:
            events = json.loads(body).get("events", [])
        except (ValueError, AttributeError):
            return Response(status_code=400)
        accepted = 0
        for ev in events if isinstance(events, list) else []:
            if not isinstance(ev, dict) or not ev.get("message_id"):
                continue
            if await agent.store.mark_seen(str(ev["message_id"])):
                accepted += 1
                task = asyncio.create_task(agent._safe(agent.handle_email_event(ev)))
                background.add(task)
                task.add_done_callback(background.discard)
        return Response(
            content=json.dumps({"accepted": accepted}), status_code=202, media_type="application/json"
        )

    @app.get("/webhook/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


def main() -> None:
    import uvicorn

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    uvicorn.run(
        create_app(),
        host=os.environ.get("WATCHER_HOST", "127.0.0.1"),
        port=int(os.environ.get("WATCHER_PORT", "8090")),
        proxy_headers=True,
    )


if __name__ == "__main__":
    main()
