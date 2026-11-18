from __future__ import annotations

import json
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from watcher import security
from watcher.app import Agent, Settings, create_app, digest_due, digest_target
from watcher.digest import LedgerEntry, format_digest, parse_inbox
from watcher.linkedin import (
    LinkedInSessionError,
    extract_threads,
    find_thread_for_sender,
    looks_like_dead_session,
)
from watcher.store import Store

SECRET = "s3cret"
CHAT = "4242"

INBOX_TEXT = """Jane Recruiter
Unread
2:15 PM
Hi! Are you open to a Staff role?

Bob Builder
Mon
You: Sounds good, talk soon

Alice Founder
Nov 12
Would love to grab coffee next week"""

INBOX_OBJ = {
    "url": "https://www.linkedin.com/messaging/",
    "sections": {"inbox": INBOX_TEXT},
    "references": {
        "inbox": [
            {"kind": "conversation", "url": "/messaging/thread/2-AAA=/", "text": "Jane Recruiter"},
            {"kind": "conversation", "url": "/messaging/thread/2-BBB/", "text": "Bob Builder"},
        ]
    },
}


# ---------- HMAC ----------
def test_hmac_roundtrip_and_rejections():
    body = b'{"events":[]}'
    ts = str(int(time.time()))
    sig = security.sign(SECRET, ts, body)
    assert security.verify(SECRET, body, ts, sig)
    assert not security.verify(SECRET, body + b" ", ts, sig)  # tampered body
    assert not security.verify("other", body, ts, sig)  # wrong secret
    assert not security.verify(SECRET, body, str(int(ts) + 1), sig)  # tampered ts
    old = str(int(time.time()) - 3600)
    assert not security.verify(SECRET, body, old, security.sign(SECRET, old, body))  # replay
    assert not security.verify(SECRET, body, None, sig)
    assert not security.verify(SECRET, body, "abc", sig)


def test_hmac_matches_apps_script_format():
    # Apps Script: 'sha256=' + hex(HMAC_SHA256(ts + '.' + body, secret))
    import hashlib
    import hmac

    expected = "sha256=" + hmac.new(b"k", b"1.{}", hashlib.sha256).hexdigest()
    assert security.sign("k", "1", b"{}") == expected


# ---------- parsing / digest ----------
def test_extract_threads_and_sender_match():
    threads = extract_threads(INBOX_OBJ)
    assert [t.thread_id for t in threads] == ["2-AAA=", "2-BBB"]
    assert find_thread_for_sender(threads, "Jane Recruiter").thread_id == "2-AAA="
    assert find_thread_for_sender(threads, "Nobody") is None
    assert find_thread_for_sender(threads, "") is None


LABELS = [("Jane Recruiter", "2-AAA="), ("Bob Builder", "2-BBB"), ("Alice Founder", "2-CCC")]


def test_parse_inbox_anchored_on_threads():
    rows = {r.name: r for r in parse_inbox(INBOX_TEXT, LABELS)}
    assert not rows["Jane Recruiter"].you_sent_last and rows["Jane Recruiter"].thread_id == "2-AAA="
    assert rows["Bob Builder"].you_sent_last
    assert not rows["Alice Founder"].you_sent_last
    # works even without blank lines between rows
    squashed = INBOX_TEXT.replace("\n\n", "\n")
    rows2 = {r.name: r.you_sent_last for r in parse_inbox(squashed, LABELS)}
    assert rows2 == {"Jane Recruiter": False, "Bob Builder": True, "Alice Founder": False}


def test_parse_inbox_fallback_without_labels():
    rows = {r.name: r.you_sent_last for r in parse_inbox(INBOX_TEXT)}
    assert rows == {"Jane Recruiter": False, "Bob Builder": True, "Alice Founder": False}


def test_format_digest():
    now = 1_000_000.0
    entries = [
        LedgerEntry("Jane Recruiter", "2-AAA=", now - 7200, 0, "Open to a Staff role?"),
        LedgerEntry("Alice Founder", None, now - 3 * 86400, now - 4 * 86400, "Coffee?"),
    ]
    text = format_digest(entries, replied_today=2, now=now)
    assert "2 awaiting your reply" in text
    assert text.index("Alice") < text.index("Jane")  # oldest first
    assert "waiting 3d" in text and "waiting 2h" in text and "[2-AAA=]" in text
    assert "replied to 2" in text
    assert format_digest([], replied_today=3) is None


async def test_ledger_tracks_in_out_and_reconciles(tmp_path):
    st = Store(str(tmp_path / "l.db"))
    await st.init()
    await st.record_incoming("Jane Recruiter", 100, "Open to a role?")
    await st.record_incoming("Bob Builder", 100, "Lunch?", thread_id="2-BBB")
    assert {r[0] for r in await st.awaiting()} == {"Jane Recruiter", "Bob Builder"}

    # reply via the bot (by thread id)
    await st.record_outgoing(200, thread_id="2-BBB")
    assert [r[0] for r in await st.awaiting()] == ["Jane Recruiter"]

    # you replied to Jane in the LinkedIn app -> daily check sees "You:" and clears it
    assert await st.reconcile("Jane Recruiter", True, "You: yes!", "2-AAA=", now=300) is True
    assert await st.awaiting() == []
    assert await st.replied_since(150) == 2

    # Bob wrote again (missed email) -> daily check sees his preview and flags it
    await st.reconcile("bob  builder", False, "Bob: still on?", "2-BBB", now=400)
    assert [(r[0], r[4]) for r in await st.awaiting()] == [("Bob Builder", "Bob: still on?")]

    # nothing changes when LinkedIn agrees with the ledger
    assert await st.reconcile("Bob Builder", False, "x", "2-BBB", now=500) is False
    assert (await st.awaiting())[0][2] == 400


def test_dead_session_detection():
    assert looks_like_dead_session("No valid LinkedIn session is available in Docker")
    assert not looks_like_dead_session("Hi, are you open to a new role?")


def test_digest_schedule():
    tz = ZoneInfo("America/Los_Angeles")
    d = date(2026, 11, 18)
    target = digest_target(d, 18, tz)
    assert target == digest_target(d, 18, tz)  # deterministic per day
    assert abs((target - datetime(2026, 11, 18, 18, tzinfo=tz)).total_seconds()) <= 15 * 60
    assert not digest_due(datetime(2026, 11, 18, 17, 0, tzinfo=tz), 18, None)
    assert digest_due(datetime(2026, 11, 18, 18, 30, tzinfo=tz), 18, None)
    assert not digest_due(datetime(2026, 11, 18, 18, 30, tzinfo=tz), 18, "2026-11-18")


# ---------- Agent with fakes ----------
class FakeLinkedIn:
    def __init__(self, dead=False):
        self.calls: list = []
        self.dead = dead

    async def inbox(self, limit=20):
        self.calls.append(("inbox", limit))
        if self.dead:
            raise LinkedInSessionError("No valid LinkedIn session")
        return INBOX_OBJ

    async def conversation(self, thread_id):
        self.calls.append(("conversation", thread_id))
        return {"sections": {"conversation": "Jane: Are you open to a Staff role?"}}

    async def reply(self, thread_id, message, confirm):
        self.calls.append(("reply", thread_id, message, confirm))
        return {"participant": "Jane Recruiter", "sent": confirm, "status": "sent" if confirm else "dry_run"}


class FakeTelegram:
    chat_id = CHAT
    enabled = True

    def __init__(self):
        self.messages: list = []
        self.answers: list = []

    async def send(self, text, buttons=None):
        self.messages.append((text, buttons))

    async def answer_callback(self, cid, text=""):
        self.answers.append(text)


class FakeDrafter:
    async def triage(self, text, instructions=""):
        return "Summary: recruiter asks about Staff role.\n1. Yes!\n2. Not now\n3. Details?"


async def _nosleep(_):
    return None


@pytest.fixture
async def agent(tmp_path):
    store = Store(str(tmp_path / "w.db"))
    await store.init()
    return Agent(
        Settings(webhook_secret=SECRET, write_cooldown_seconds=0, write_daily_cap=5),
        store,
        FakeLinkedIn(),
        FakeTelegram(),
        FakeDrafter(),
        sleep=_nosleep,
    )


async def test_email_event_fetches_once_and_notifies(agent):
    await agent.handle_email_event({"message_id": "m1", "from_name": "Jane Recruiter", "snippet": "Hi"})
    assert agent.li.calls == [("inbox", 10), ("conversation", "2-AAA=")]
    text, _ = agent.tg.messages[-1]
    assert "Jane Recruiter" in text and "/send 2-AAA=" in text and "Summary" in text
    assert not any(c[0] == "reply" for c in agent.li.calls)  # never auto-replies
    assert [r[0] for r in await agent.store.awaiting()] == ["Jane Recruiter"]


async def test_email_event_dead_session_falls_back_to_snippet(agent):
    agent.li.dead = True
    await agent.handle_email_event({"message_id": "m2", "from_name": "Jane", "snippet": "Hello there"})
    text, _ = agent.tg.messages[-1]
    assert "Hello there" in text and "Re-login" in text


async def test_send_requires_confirmation_button(agent):
    upd = {"message": {"chat": {"id": int(CHAT)}, "text": "/send 2-AAA= Yes, happy to chat!"}}
    await agent.handle_update(upd)
    # only a dry run so far
    assert agent.li.calls == [("reply", "2-AAA=", "Yes, happy to chat!", False)]
    text, buttons = agent.tg.messages[-1]
    assert "Jane Recruiter" in text
    data = buttons[0][0]["callback_data"]
    assert data.startswith("send:")

    # a stranger pressing the button does nothing
    await agent.handle_update({"callback_query": {"id": "x", "data": data, "message": {"chat": {"id": 1}}}})
    assert len(agent.li.calls) == 1

    await agent.handle_update(
        {"callback_query": {"id": "c1", "data": data, "message": {"chat": {"id": int(CHAT)}}}}
    )
    assert agent.li.calls[-1] == ("reply", "2-AAA=", "Yes, happy to chat!", True)
    assert agent.tg.messages[-1][0].startswith("✅ Sent")

    # replaying the same confirmation does not send again
    await agent.handle_update(
        {"callback_query": {"id": "c2", "data": data, "message": {"chat": {"id": int(CHAT)}}}}
    )
    assert sum(1 for c in agent.li.calls if c[0] == "reply" and c[3]) == 1


async def test_cancel_button(agent):
    await agent.handle_command("/send 2-BBB hi")
    data = agent.tg.messages[-1][1][0][1]["callback_data"]
    assert data.startswith("cancel:")
    await agent.handle_callback("c", data)
    assert not any(c[0] == "reply" and c[3] for c in agent.li.calls)


async def test_strangers_are_ignored(agent):
    await agent.handle_update({"message": {"chat": {"id": 999}, "text": "/inbox"}})
    assert agent.li.calls == [] and agent.tg.messages == []


async def test_digest_uses_ledger_and_inbox(agent):
    # Jane emailed (ledger: incoming). LinkedIn inbox: Jane & Alice wrote last, Bob = "You:".
    await agent.store.record_incoming("Jane Recruiter", time.time() - 3600, "Staff role?")
    text = await agent.run_digest()
    assert "Jane Recruiter" in text and "Alice Founder" in text and "Bob Builder" not in text

    # You reply to both in the LinkedIn app -> next day's inbox shows "You:" -> nothing pending
    agent.li.inbox = lambda limit=20: _async(
        {
            "sections": {"inbox": "Jane Recruiter\nYou: Sure!\n\nAlice Founder\nYou: Tuesday works"},
            "references": INBOX_OBJ["references"],
        }
    )
    n = len(agent.tg.messages)
    assert await agent.run_digest() is None
    assert len(agent.tg.messages) == n


async def test_done_command_clears_thread(agent):
    await agent.store.record_incoming("Jane Recruiter", 100, "hi", thread_id="2-AAA=")
    await agent.handle_command("/done 2-AAA=")
    assert await agent.store.awaiting() == []


async def test_bot_send_records_outgoing(agent):
    await agent.store.record_incoming("Jane Recruiter", 100, "hi", thread_id="2-AAA=")
    await agent.handle_command("/send 2-AAA= Sounds great")
    data = agent.tg.messages[-1][1][0][0]["callback_data"]
    await agent.handle_callback("c", data)
    assert await agent.store.awaiting() == []


async def _async(v):
    return v


# ---------- webhook HTTP ----------
def test_webhook_http(tmp_path):
    import asyncio

    store = Store(str(tmp_path / "h.db"))
    ag = Agent(
        Settings(webhook_secret=SECRET), store, FakeLinkedIn(), FakeTelegram(), FakeDrafter(), sleep=_nosleep
    )
    asyncio.run(store.init())
    app = create_app(ag, start_background=False)
    body = json.dumps(
        {"events": [{"message_id": "abc", "from_name": "Jane Recruiter", "snippet": "Hi"}]}
    ).encode()
    ts = str(int(time.time()))
    headers = {
        "X-Timestamp": ts,
        "X-Signature-256": security.sign(SECRET, ts, body),
        "content-type": "application/json",
    }
    with TestClient(app) as client:
        assert (
            client.post(
                "/webhook/gmail", content=body, headers={**headers, "X-Signature-256": "sha256=bad"}
            ).status_code
            == 401
        )
        r = client.post("/webhook/gmail", content=body, headers=headers)
        assert r.status_code == 202 and r.json() == {"accepted": 1}
        r = client.post("/webhook/gmail", content=body, headers=headers)  # duplicate
        assert r.json() == {"accepted": 0}
        assert client.get("/webhook/health").json() == {"status": "ok"}
