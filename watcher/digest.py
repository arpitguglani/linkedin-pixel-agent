"""Daily digest: classify inbox rows into unread / awaiting-reply and format."""

from __future__ import annotations

import re
from dataclasses import dataclass

_DATE_RE = re.compile(
    r"^(\d{1,2}:\d{2}\s?(am|pm)?|mon|tue|wed|thu|fri|sat|sun|today|yesterday|"
    r"[a-z]{3}\s\d{1,2}(,\s\d{4})?|\d{1,2}/\d{1,2}(/\d{2,4})?|\d+[mhdw])$",
    re.IGNORECASE,
)
_NOISE = {"active now", "status is online", "status is reachable", "sponsored", "inmail"}


@dataclass
class InboxRow:
    name: str
    preview: str
    unread: bool
    awaiting_reply: bool


def parse_inbox_text(text: str) -> list[InboxRow]:
    """Best-effort parse of LinkedIn inbox innerText into rows.

    Rows are separated by blank lines; within a row the first meaningful line
    is the participant name and the last is the message preview. A preview
    starting with "You:" means you sent the last message.
    """
    rows: list[InboxRow] = []
    for block in re.split(r"\n\s*\n", text or ""):
        lines = [ln.strip() for ln in block.splitlines() if ln.strip()]
        unread = any("unread" in ln.lower() for ln in lines)
        lines = [
            ln
            for ln in lines
            if not _DATE_RE.match(ln) and ln.lower() not in _NOISE and "unread" not in ln.lower()
        ]
        if len(lines) < 2:
            continue
        name, preview = lines[0], lines[-1]
        you_last = preview.lower().startswith("you:")
        rows.append(InboxRow(name=name, preview=preview, unread=unread, awaiting_reply=not you_last))
    return rows


def format_digest(rows: list[InboxRow], max_items: int = 10) -> str | None:
    """Returns the Telegram digest text, or None if nothing needs attention."""
    unread = [r for r in rows if r.unread]
    awaiting = [r for r in rows if r.awaiting_reply and not r.unread]
    if not unread and not awaiting:
        return None

    def fmt(r: InboxRow) -> str:
        preview = r.preview if len(r.preview) <= 90 else r.preview[:87] + "..."
        return f"• {r.name}: {preview}"

    parts = ["📬 LinkedIn daily digest"]
    if unread:
        parts.append(f"\n🔴 Unread ({len(unread)}):")
        parts += [fmt(r) for r in unread[:max_items]]
    if awaiting:
        parts.append(f"\n⏳ Awaiting your reply ({len(awaiting)}):")
        parts += [fmt(r) for r in awaiting[:max_items]]
    parts.append("\nUse /inbox to list threads, /read <id> to open one.")
    return "\n".join(parts)
