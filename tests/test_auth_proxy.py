from __future__ import annotations

import base64
import os
from types import SimpleNamespace

import pytest
from fastmcp import Client, FastMCP
from fastmcp.server import create_proxy
from starlette.testclient import TestClient

from auth_proxy.policy import (
    PreviewRequired,
    RateLimitExceeded,
    SendGate,
    WriteRateLimiter,
    is_allowed_identity,
    parse_tool_list,
)
from auth_proxy.proxy import GuardMiddleware, build_server, make_identity_check

ALLOWED = "Me@Gmail.com"


# ---------- identity allowlist ----------
@pytest.mark.parametrize(
    "claims,expected",
    [
        ({"email": "me@gmail.com", "email_verified": True}, True),
        ({"email": " ME@gmail.com ", "email_verified": "true"}, True),
        ({"email": "me@gmail.com", "email_verified": False}, False),
        ({"email": "me@gmail.com", "email_verified": "false"}, False),
        ({"email": "me@gmail.com"}, False),
        ({"email": "attacker@gmail.com", "email_verified": True}, False),
        ({"email": "me@gmail.com.evil.com", "email_verified": True}, False),
        ({}, False),
        (None, False),
    ],
)
def test_identity_allowlist(claims, expected):
    assert is_allowed_identity(claims, ALLOWED) is expected


def test_identity_check_callable():
    check = make_identity_check(ALLOWED)
    tok = SimpleNamespace(claims={"email": "me@gmail.com", "email_verified": True})
    assert check(SimpleNamespace(token=tok)) is True
    assert check(SimpleNamespace(token=None)) is False


def test_empty_allowed_email_denies_everyone():
    assert not is_allowed_identity({"email": "", "email_verified": True}, "")


# ---------- rate limiter & send gate ----------
class Clock:
    def __init__(self):
        self.t = 1000.0

    def __call__(self):
        return self.t


def test_rate_limiter_cooldown_and_cap():
    clock = Clock()
    rl = WriteRateLimiter(cooldown_seconds=60, daily_cap=3, clock=clock)
    rl.check_and_record()
    with pytest.raises(RateLimitExceeded):
        rl.check_and_record()
    clock.t += 61
    rl.check_and_record()
    clock.t += 61
    rl.check_and_record()
    clock.t += 61
    with pytest.raises(RateLimitExceeded, match="Daily"):
        rl.check_and_record()
    clock.t += 86_400
    rl.check_and_record()


def test_send_gate_requires_matching_preview():
    clock = Clock()
    gate = SendGate(ttl_seconds=900, clock=clock)
    send = {"thread_id": "t1", "message": "Hi!", "confirm_send": True}
    with pytest.raises(PreviewRequired):
        gate.check("reply_to_conversation", send)
    assert gate.check("reply_to_conversation", {**send, "confirm_send": False}) is False
    # different message -> still blocked
    with pytest.raises(PreviewRequired):
        gate.check("reply_to_conversation", {**send, "message": "Other"})
    assert gate.check("reply_to_conversation", send) is True
    # one preview authorizes exactly one send
    with pytest.raises(PreviewRequired):
        gate.check("reply_to_conversation", send)
    # preview expires
    gate.check("reply_to_conversation", {**send, "confirm_send": False})
    clock.t += 901
    with pytest.raises(PreviewRequired):
        gate.check("reply_to_conversation", send)


def test_parse_tool_list():
    assert "get_inbox" in parse_tool_list(None)
    assert parse_tool_list("a, b") == frozenset({"a", "b"})


# ---------- middleware against an in-memory fake LinkedIn MCP ----------
def fake_upstream() -> tuple[FastMCP, list]:
    sent: list = []
    up = FastMCP("fake-linkedin")

    @up.tool
    def get_inbox(limit: int = 20) -> dict:
        return {"sections": {"inbox": "Jane\nHello"}}

    @up.tool
    def reply_to_conversation(thread_id: str, message: str, confirm_send: bool) -> dict:
        if confirm_send:
            sent.append((thread_id, message))
        return {"sent": confirm_send, "status": "sent" if confirm_send else "dry_run"}

    @up.tool
    def get_person_profile(linkedin_username: str) -> dict:
        return {}

    return up, sent


async def test_guard_middleware_filters_and_gates():
    up, sent = fake_upstream()
    clock = Clock()
    guard = GuardMiddleware(
        frozenset({"get_inbox", "reply_to_conversation"}),
        WriteRateLimiter(60, 5, clock=clock),
        SendGate(clock=clock),
    )
    proxy = create_proxy(up, middleware=[guard])
    async with Client(proxy) as c:
        names = {t.name for t in await c.list_tools()}
        assert names == {"get_inbox", "reply_to_conversation"}

        res = await c.call_tool("get_person_profile", {"linkedin_username": "x"}, raise_on_error=False)
        assert res.is_error

        args = {"thread_id": "t1", "message": "Thanks!", "confirm_send": True}
        res = await c.call_tool("reply_to_conversation", args, raise_on_error=False)
        assert res.is_error and not sent

        await c.call_tool("reply_to_conversation", {**args, "confirm_send": False})
        await c.call_tool("reply_to_conversation", args)
        assert sent == [("t1", "Thanks!")]

        # cooldown on a second real send
        await c.call_tool("reply_to_conversation", {**args, "message": "2", "confirm_send": False})
        res = await c.call_tool("reply_to_conversation", {**args, "message": "2"}, raise_on_error=False)
        assert res.is_error and len(sent) == 1


# ---------- full HTTP app with Google OAuth ----------
@pytest.fixture
def oauth_app(tmp_path, monkeypatch):
    env = {
        "GOOGLE_CLIENT_ID": "fake.apps.googleusercontent.com",
        "GOOGLE_CLIENT_SECRET": "fake-secret",
        "MCP_DOMAIN": "agent.example.com",
        "ALLOWED_EMAIL": ALLOWED,
        "JWT_SIGNING_KEY": "x" * 48,
        "STORAGE_ENCRYPTION_KEY": base64.urlsafe_b64encode(os.urandom(32)).decode(),
        "OAUTH_STORAGE_DIR": str(tmp_path / "oauth"),
    }
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    up, _ = fake_upstream()
    return build_server_with(up).http_app(path="/mcp")


def build_server_with(upstream):
    from auth_proxy import proxy as mod

    return mod.create_proxy(
        upstream,
        auth=mod.build_auth(),
        middleware=[mod.AuthMiddleware(auth=make_identity_check(ALLOWED))],
    )


def test_unauthenticated_mcp_is_rejected(oauth_app):
    with TestClient(oauth_app, base_url="https://agent.example.com") as client:
        r = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            headers={"accept": "application/json, text/event-stream"},
        )
        assert r.status_code == 401
        assert "resource_metadata" in r.headers.get("www-authenticate", "")


def test_oauth_metadata_and_dcr(oauth_app):
    with TestClient(oauth_app, base_url="https://agent.example.com") as client:
        meta = client.get("/.well-known/oauth-authorization-server").json()
        assert meta["issuer"].startswith("https://agent.example.com")
        assert meta["registration_endpoint"].endswith("/register")
        assert "S256" in meta["code_challenge_methods_supported"]

        prm = client.get("/.well-known/oauth-protected-resource/mcp")
        assert prm.status_code == 200
        assert prm.json()["resource"].startswith("https://agent.example.com")

        reg = client.post(
            "/register",
            json={
                "client_name": "Claude",
                "redirect_uris": ["https://claude.ai/api/mcp/auth_callback"],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "none",
            },
        )
        assert reg.status_code == 201, reg.text
        assert reg.json()["client_id"]


def test_build_server_from_env(oauth_app):
    # build_server wires auth + both middlewares without raising
    server = build_server(upstream_url="http://127.0.0.1:9/mcp")
    assert server.auth is not None
