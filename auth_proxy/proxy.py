"""OAuth-protected public front door for the LinkedIn MCP server.

Claude / Gemini  --HTTPS-->  Caddy  -->  this proxy (127.0.0.1:8080/mcp)
                                          |  Google OAuth (DCR) + allowlist
                                          v
                              linkedin-mcp (127.0.0.1:8000/mcp)
"""

from __future__ import annotations

import logging
import os
from collections.abc import Sequence
from typing import Any

from fastmcp.exceptions import ToolError
from fastmcp.server import create_proxy
from fastmcp.server.auth.providers.google import GoogleProvider
from fastmcp.server.dependencies import get_access_token
from fastmcp.server.middleware import Middleware, MiddlewareContext
from fastmcp.server.middleware.authorization import AuthMiddleware
from fastmcp.utilities.authorization import AuthContext

from auth_proxy.policy import (
    WRITE_TOOLS,
    PreviewRequired,
    RateLimitExceeded,
    SendGate,
    WriteRateLimiter,
    is_allowed_identity,
    is_allowed_tool,
    parse_tool_list,
)

log = logging.getLogger("auth_proxy")


def _env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None or value == "":
        raise RuntimeError(f"Missing required environment variable {name}")
    return value


def make_identity_check(allowed_email: str):
    def check(ctx: AuthContext) -> bool:
        claims = getattr(ctx.token, "claims", None) if ctx.token else None
        return is_allowed_identity(claims, allowed_email)

    return check


class GuardMiddleware(Middleware):
    """Tool allowlist + preview-before-send gate + write rate limiting.

    Logs tool names only; never logs message content.
    """

    def __init__(
        self,
        allowed_tools: frozenset[str],
        limiter: WriteRateLimiter,
        gate: SendGate,
    ) -> None:
        self.allowed_tools = allowed_tools
        self.limiter = limiter
        self.gate = gate

    async def on_list_tools(self, context: MiddlewareContext, call_next) -> Sequence[Any]:
        tools = await call_next(context)
        return [t for t in tools if is_allowed_tool(t.name, self.allowed_tools)]

    async def on_call_tool(self, context: MiddlewareContext, call_next):
        name = context.message.name
        args = dict(context.message.arguments or {})
        if not is_allowed_tool(name, self.allowed_tools):
            log.warning("blocked tool=%s (not allowlisted)", name)
            raise ToolError(f"Tool '{name}' is disabled on this server.")
        if name in WRITE_TOOLS:
            try:
                is_real_send = self.gate.check(name, args)
                if is_real_send:
                    self.limiter.check_and_record()
            except (PreviewRequired, RateLimitExceeded) as exc:
                log.warning("blocked write tool=%s reason=%s", name, type(exc).__name__)
                raise ToolError(str(exc)) from exc
            log.info("write tool=%s real_send=%s", name, is_real_send)
        else:
            log.info("read tool=%s", name)
        return await call_next(context)


def build_auth() -> GoogleProvider:
    from cryptography.fernet import Fernet
    from key_value.aio.stores.filetree import FileTreeStore
    from key_value.aio.wrappers.encryption import FernetEncryptionWrapper

    storage_dir = os.environ.get("OAUTH_STORAGE_DIR", "/data/oauth")
    storage = FernetEncryptionWrapper(
        FileTreeStore(data_directory=storage_dir),
        fernet=Fernet(_env("STORAGE_ENCRYPTION_KEY")),
    )
    return GoogleProvider(
        client_id=_env("GOOGLE_CLIENT_ID"),
        client_secret=_env("GOOGLE_CLIENT_SECRET"),
        base_url=f"https://{_env('MCP_DOMAIN')}",
        required_scopes=["openid", "https://www.googleapis.com/auth/userinfo.email"],
        jwt_signing_key=_env("JWT_SIGNING_KEY"),
        client_storage=storage,
    )


def build_server(auth=None, upstream_url: str | None = None, **overrides):
    """Builds the proxy. ``auth`` defaults to Google OAuth from env vars."""
    allowed_email = overrides.get("allowed_email") or _env("ALLOWED_EMAIL")
    allowed_tools = parse_tool_list(os.environ.get("ALLOWED_TOOLS"))
    limiter = overrides.get("limiter") or WriteRateLimiter(
        cooldown_seconds=float(os.environ.get("WRITE_COOLDOWN_SECONDS", "60")),
        daily_cap=int(os.environ.get("WRITE_DAILY_CAP", "20")),
    )
    gate = overrides.get("gate") or SendGate()
    upstream = upstream_url or os.environ.get("LINKEDIN_MCP_URL", "http://127.0.0.1:8000/mcp")
    return create_proxy(
        upstream,
        name="LinkedIn Messages (private)",
        auth=auth if auth is not None else build_auth(),
        middleware=[
            AuthMiddleware(auth=make_identity_check(allowed_email)),
            GuardMiddleware(allowed_tools, limiter, gate),
        ],
    )


def whoami_email() -> str | None:
    token = get_access_token()
    return (token.claims or {}).get("email") if token else None


def main() -> None:
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"))
    server = build_server()
    server.run(
        transport="http",
        host=os.environ.get("PROXY_HOST", "127.0.0.1"),
        port=int(os.environ.get("PROXY_PORT", "8080")),
        path="/mcp",
    )


if __name__ == "__main__":
    main()
