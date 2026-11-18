from __future__ import annotations

import json
import time
from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
from starlette.testclient import TestClient

from watcher import security
from watcher.app import Agent, Settings, create_app, digest_due, digest_target
from watcher.digest import format_digest, parse_inbox_text
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


def test_parse_and_classify_inbox():
    rows = parse_inbox_text(INBOX_TEXT)
    by = {r.name: r for r in rows}
    assert by["Jane Recruiter"].unread and by["Jane Recruiter"].awaiting_reply
    assert not by["Bob Builder"].awaiting_reply
    assert by["Alice Founder"].awaiting_reply and not by["Alice Founder"].unread


def test_format_digest():
    text = format_digest(parse_inbox_text(INBOX_TEXT))
    assert "Unread (1)" in text and "Jane Recruiter" in text
    assert "Awaiting your reply (1)" in text and "Alice Founder" in text
    assert "Bob Builder" not in text
    assert format_digest(parse_inbox_text("Bob\nMon\nYou: ok")) is None
    assert format_digest([]) is None


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


async def test_digest_sends_only_when_needed(agent):
    assert "Unread" in (await agent.run_digest())
    agent.li.inbox = lambda limit=20: _async({"sections": {"inbox": "Bob\nMon\nYou: ok"}})
    n = len(agent.tg.messages)
    assert await agent.run_digest() is None
    assert len(agent.tg.messages) == n


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
