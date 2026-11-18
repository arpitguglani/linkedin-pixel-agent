"""Pure, dependency-free policy logic for the auth proxy (easy to unit test)."""

from __future__ import annotations

import hashlib
import time
from collections import deque
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

# Only the DM/InMail tools are exposed to Claude/Gemini. Everything else the
# upstream server offers (profiles, jobs, feed, connect...) is hidden.
DEFAULT_ALLOWED_TOOLS: frozenset[str] = frozenset(
    {
        "get_inbox",
        "get_conversation",
        "search_conversations",
        "reply_to_conversation",
        "get_session_status",
    }
)

# Tools that can write to LinkedIn. send_message is intentionally NOT in the
# default allowlist (upstream bug #483 can create duplicate threads).
WRITE_TOOLS: frozenset[str] = frozenset({"send_message", "reply_to_conversation"})


def _truthy(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def is_allowed_identity(claims: Mapping[str, Any] | None, allowed_email: str) -> bool:
    """True only for the single allowlisted, Google-verified email address."""
    if not claims or not allowed_email:
        return False
    email = claims.get("email")
    if not isinstance(email, str) or not email:
        return False
    if not _truthy(claims.get("email_verified")):
        return False
    return email.strip().lower() == allowed_email.strip().lower()


def is_allowed_tool(name: str, allowed_tools: frozenset[str] = DEFAULT_ALLOWED_TOOLS) -> bool:
    return name in allowed_tools


def parse_tool_list(raw: str | None) -> frozenset[str]:
    if not raw:
        return DEFAULT_ALLOWED_TOOLS
    return frozenset(t.strip() for t in raw.split(",") if t.strip())


class RateLimitExceeded(Exception):
    pass


@dataclass
class WriteRateLimiter:
    """Cooldown between sends plus a rolling 24h cap."""

    cooldown_seconds: float = 60.0
    daily_cap: int = 20
    clock: Callable[[], float] = time.monotonic
    _events: deque[float] = field(default_factory=deque)

    def check_and_record(self) -> None:
        now = self.clock()
        while self._events and now - self._events[0] >= 86_400:
            self._events.popleft()
        if self._events and now - self._events[-1] < self.cooldown_seconds:
            wait = int(self.cooldown_seconds - (now - self._events[-1])) + 1
            raise RateLimitExceeded(f"Write cooldown active; retry in ~{wait}s.")
        if len(self._events) >= self.daily_cap:
            raise RateLimitExceeded(f"Daily write cap of {self.daily_cap} reached.")
        self._events.append(now)


class PreviewRequired(Exception):
    pass


def _fingerprint(tool: str, args: Mapping[str, Any]) -> str:
    target = str(args.get("thread_id") or args.get("linkedin_username") or "")
    message = str(args.get("message") or "").strip()
    return hashlib.sha256(f"{tool}\x00{target}\x00{message}".encode()).hexdigest()


@dataclass
class SendGate:
    """Human-in-the-loop guard for write tools.

    A real send (confirm_send=True) is only allowed if the exact same
    tool+target+message was first previewed as a dry run (confirm_send=False)
    within ``ttl_seconds``. This forces the assistant to show you the draft and
    recipient before anything is sent, on top of the Claude/Gemini app's own
    confirmation prompt. Each preview authorizes exactly one send.
    """

    ttl_seconds: float = 900.0
    clock: Callable[[], float] = time.monotonic
    _previews: dict[str, float] = field(default_factory=dict)

    def check(self, tool: str, args: Mapping[str, Any]) -> bool:
        """Returns True if this is a real send that is allowed to proceed,
        False if it is a dry run. Raises PreviewRequired otherwise."""
        now = self.clock()
        self._previews = {k: t for k, t in self._previews.items() if now - t < self.ttl_seconds}
        fp = _fingerprint(tool, args)
        if not _truthy(args.get("confirm_send")):
            self._previews[fp] = now
            return False
        if fp not in self._previews:
            raise PreviewRequired(
                "Send blocked: first call this tool with confirm_send=false using the exact "
                "same thread and message, show the user the preview, and only send after the "
                "user explicitly approves."
            )
        del self._previews[fp]
        return True
