"""Inbox reading + daily digest.

"Awaiting reply" is decided from a ledger, not guessed:
  * every incoming message reported by the Gmail trigger is recorded,
  * every reply sent through this system is recorded,
  * once a day the LinkedIn inbox is read and each thread is reconciled using
    LinkedIn's own marker: a row whose preview starts with "You:" means YOU sent
    the last message (e.g. you replied in the LinkedIn app); otherwise the other
    person did.
A thread is awaiting your reply iff its last incoming message is newer than
your last outgoing one.
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

_DATE_RE = re.compile(
    r"^(\d{1,2}:\d{2}\s?(am|pm)?|mon|tue|wed|thu|fri|sat|sun|today|yesterday|"
    r"[a-z]{3}\s\d{1,2}(,\s\d{4})?|\d{1,2}/\d{1,2}(/\d{2,4})?|\d+[mhdw])$",
    re.IGNORECASE,
)
_NOISE = {"active now", "status is online", "status is reachable", "sponsored", "inmail", "unread"}


@dataclass
class InboxRow:
    name: str
    preview: str
    you_sent_last: bool
    thread_id: str | None = None


@dataclass
class LedgerEntry:
    name: str
    thread_id: str | None
    last_in: float
    last_out: float
    preview: str

    @property
    def awaiting_reply(self) -> bool:
        return self.last_in > self.last_out


def _meaningful(lines: list[str]) -> list[str]:
    return [ln for ln in lines if ln and not _DATE_RE.match(ln) and ln.lower() not in _NOISE]


def _row_from_lines(name: str, lines: list[str], thread_id: str | None = None) -> InboxRow | None:
    """A row is: name, (date/status noise), preview line. The preview is the
    first meaningful line after the name; LinkedIn prefixes it with "You:" when
    you sent the last message."""
    body = [ln for ln in _meaningful(lines) if ln.lower() != name.lower()]
    if not body:
        return None
    preview = body[0]
    return InboxRow(
        name=name, preview=preview, you_sent_last=preview.lower().startswith("you:"), thread_id=thread_id
    )


def parse_inbox(text: str, labelled_threads: list[tuple[str, str]] | None = None) -> list[InboxRow]:
    """Split inbox text into rows.

    When thread references with participant names are available (the normal
    case), each row is the text between one participant's name and the next,
    so rows can't bleed into each other. Otherwise falls back to blank-line
    separated blocks.
    """
    text = text or ""
    rows: list[InboxRow] = []
    anchors: list[tuple[int, str, str]] = []
    pos = 0
    for label, thread_id in labelled_threads or []:
        if not label:
            continue
        idx = text.find(label, pos)
        if idx < 0:
            idx = text.find(label)
        if idx >= 0:
            anchors.append((idx, label, thread_id))
            pos = idx + len(label)
    anchors.sort()
    for i, (start, label, thread_id) in enumerate(anchors):
        end = anchors[i + 1][0] if i + 1 < len(anchors) else len(text)
        seg = [ln.strip() for ln in text[start:end].splitlines()]
        row = _row_from_lines(label, seg, thread_id)
        if row:
            rows.append(row)
    # Rows the reference scan didn't cover (it can stop early): blank-line blocks.
    known = {r.name.lower() for r in rows}
    for block in re.split(r"\n\s*\n", text):
        lines = _meaningful([ln.strip() for ln in block.splitlines()])
        if len(lines) >= 2 and lines[0].lower() not in known:
            row = _row_from_lines(lines[0], lines[1:])
            if row:
                rows.append(row)
    return rows


def _ago(seconds: float) -> str:
    if seconds < 3600:
        return f"{max(1, int(seconds // 60))}m"
    if seconds < 86400:
        return f"{int(seconds // 3600)}h"
    return f"{int(seconds // 86400)}d"


def format_digest(
    awaiting: list[LedgerEntry],
    replied_today: int = 0,
    now: float | None = None,
    max_items: int = 15,
) -> str | None:
    """Telegram digest text, or None when nothing is waiting on you."""
    if not awaiting:
        return None
    now = time.time() if now is None else now
    awaiting = sorted(awaiting, key=lambda e: e.last_in)
    parts = [f"📬 LinkedIn daily digest — {len(awaiting)} awaiting your reply"]
    for e in awaiting[:max_items]:
        preview = e.preview if len(e.preview) <= 90 else e.preview[:87] + "..."
        tid = f" [{e.thread_id}]" if e.thread_id else ""
        parts.append(f"• {e.name} (waiting {_ago(now - e.last_in)}){tid}: {preview}")
    if len(awaiting) > max_items:
        parts.append(f"…and {len(awaiting) - max_items} more")
    if replied_today:
        parts.append(f"\n✅ You replied to {replied_today} thread(s) since the last digest.")
    parts.append("\n/read <id> to open · /draft <id> for AI replies")
    return "\n".join(parts)
