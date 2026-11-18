"""Talks to the local linkedin-mcp server and parses its (raw-text) results."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any

THREAD_RE = re.compile(r"/messaging/thread/([^/\s\"'?#\\]+)")
SESSION_DEAD_MARKERS = (
    "no valid linkedin session",
    "session expired",
    "not logged in",
    "login required",
    "checkpoint",
    "authwall",
)


class LinkedInSessionError(RuntimeError):
    """LinkedIn session is gone; a re-login via the noVNC viewer is needed."""


@dataclass
class ThreadRef:
    thread_id: str
    label: str = ""


def result_to_obj(result: Any) -> Any:
    """Normalise a fastmcp CallToolResult into plain Python data."""
    for attr in ("structured_content", "data"):
        value = getattr(result, attr, None)
        if value:
            return value
    texts = [getattr(c, "text", "") for c in getattr(result, "content", []) or []]
    joined = "\n".join(t for t in texts if t)
    try:
        return json.loads(joined)
    except (ValueError, TypeError):
        return {"sections": {"text": joined}}


def looks_like_dead_session(text: str) -> bool:
    lowered = text.lower()
    return any(marker in lowered for marker in SESSION_DEAD_MARKERS)


def section_text(obj: Any) -> str:
    if isinstance(obj, dict):
        sections = obj.get("sections")
        if isinstance(sections, dict):
            return "\n\n".join(str(v) for v in sections.values() if v)
    return obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)


def _iter_references(obj: Any):
    refs = obj.get("references") if isinstance(obj, dict) else None
    if isinstance(refs, dict):
        for value in refs.values():
            if isinstance(value, list):
                yield from (r for r in value if isinstance(r, dict))
    elif isinstance(refs, list):
        yield from (r for r in refs if isinstance(r, dict))


def extract_threads(obj: Any) -> list[ThreadRef]:
    """Thread ids (deduped, ordered) with human labels when available."""
    found: dict[str, ThreadRef] = {}
    for ref in _iter_references(obj):
        m = THREAD_RE.search(str(ref.get("url", "")) + " ")
        if m and m.group(1) not in found:
            found[m.group(1)] = ThreadRef(m.group(1), str(ref.get("text") or ""))
    blob = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
    for m in THREAD_RE.finditer(blob):
        found.setdefault(m.group(1), ThreadRef(m.group(1)))
    return list(found.values())


def find_thread_for_sender(threads: list[ThreadRef], sender: str) -> ThreadRef | None:
    sender_l = sender.strip().lower()
    if not sender_l:
        return None
    for t in threads:
        if t.label and (sender_l in t.label.lower() or t.label.lower() in sender_l):
            return t
    return None


class LinkedInMCP:
    """Minimal async wrapper over the local (unauthenticated, loopback) MCP."""

    def __init__(self, url: str) -> None:
        self.url = url

    async def call(self, tool: str, args: dict[str, Any] | None = None) -> Any:
        from fastmcp import Client

        async with Client(self.url, timeout=180) as client:
            result = await client.call_tool(tool, args or {}, raise_on_error=False)
        obj = result_to_obj(result)
        text = section_text(obj)
        if getattr(result, "is_error", False):
            if looks_like_dead_session(text):
                raise LinkedInSessionError(text[:300])
            raise RuntimeError(f"{tool} failed: {text[:300]}")
        if looks_like_dead_session(text) and len(text) < 600:
            raise LinkedInSessionError(text[:300])
        return obj

    async def inbox(self, limit: int = 20) -> Any:
        return await self.call("get_inbox", {"limit": limit})

    async def conversation(self, thread_id: str) -> Any:
        return await self.call("get_conversation", {"thread_id": thread_id})

    async def reply(self, thread_id: str, message: str, confirm: bool) -> Any:
        return await self.call(
            "reply_to_conversation",
            {"thread_id": thread_id, "message": message, "confirm_send": confirm},
        )
