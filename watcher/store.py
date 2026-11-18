"""Tiny SQLite store: webhook dedupe, pending sends, digest bookkeeping."""

from __future__ import annotations

import secrets
import time

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS seen_events (id TEXT PRIMARY KEY, created_at REAL NOT NULL);
CREATE TABLE IF NOT EXISTS pending_sends (
    token TEXT PRIMARY KEY, thread_id TEXT NOT NULL, message TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS kv (k TEXT PRIMARY KEY, v TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS ledger (
    contact TEXT PRIMARY KEY, name TEXT NOT NULL, thread_id TEXT,
    last_in REAL NOT NULL DEFAULT 0, last_out REAL NOT NULL DEFAULT 0,
    preview TEXT NOT NULL DEFAULT ''
);
"""


def _contact_key(name: str) -> str:
    return " ".join(name.lower().split())


class Store:
    def __init__(self, path: str) -> None:
        self.path = path

    async def init(self) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.executescript(SCHEMA)
            await db.commit()

    # ---------- message ledger ----------
    async def _upsert(self, db, name: str, thread_id: str | None) -> str:
        key = _contact_key(name)
        await db.execute(
            "INSERT INTO ledger (contact, name, thread_id) VALUES (?, ?, ?) "
            "ON CONFLICT(contact) DO UPDATE SET thread_id = COALESCE(excluded.thread_id, thread_id)",
            (key, name.strip(), thread_id),
        )
        return key

    async def record_incoming(
        self, name: str, at: float, preview: str = "", thread_id: str | None = None
    ) -> None:
        if not name.strip():
            return
        async with aiosqlite.connect(self.path) as db:
            key = await self._upsert(db, name, thread_id)
            await db.execute(
                "UPDATE ledger SET last_in = MAX(last_in, ?), "
                "preview = CASE WHEN ? >= last_in AND ? != '' THEN ? ELSE preview END WHERE contact = ?",
                (at, at, preview, preview, key),
            )
            await db.commit()

    async def record_outgoing(self, at: float, name: str | None = None, thread_id: str | None = None) -> None:
        async with aiosqlite.connect(self.path) as db:
            if thread_id:
                cur = await db.execute(
                    "UPDATE ledger SET last_out = MAX(last_out, ?) WHERE thread_id = ?", (at, thread_id)
                )
                if cur.rowcount or not name:
                    await db.commit()
                    return
            if name and name.strip():
                key = await self._upsert(db, name, thread_id)
                await db.execute("UPDATE ledger SET last_out = MAX(last_out, ?) WHERE contact = ?", (at, key))
            await db.commit()

    async def reconcile(
        self, name: str, you_sent_last: bool, preview: str, thread_id: str | None, now: float
    ) -> bool:
        """Align the ledger with LinkedIn's truth for one thread.

        Returns True if this reconciliation discovered a reply you made outside
        this system (e.g. in the LinkedIn app)."""
        async with aiosqlite.connect(self.path) as db:
            key = await self._upsert(db, name, thread_id)
            cur = await db.execute("SELECT last_in, last_out FROM ledger WHERE contact = ?", (key,))
            last_in, last_out = await cur.fetchone()
            replied_elsewhere = False
            if you_sent_last and last_out < last_in:
                await db.execute("UPDATE ledger SET last_out = ? WHERE contact = ?", (now, key))
                replied_elsewhere = True
            elif not you_sent_last and last_in <= last_out:
                await db.execute(
                    "UPDATE ledger SET last_in = ?, preview = ? WHERE contact = ?", (now, preview, key)
                )
            await db.commit()
        return replied_elsewhere

    async def awaiting(self) -> list[tuple[str, str | None, float, float, str]]:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT name, thread_id, last_in, last_out, preview FROM ledger "
                "WHERE last_in > last_out ORDER BY last_in"
            )
            return list(await cur.fetchall())

    async def replied_since(self, since: float) -> int:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT COUNT(*) FROM ledger WHERE last_out >= ? AND last_out >= last_in", (since,)
            )
            return (await cur.fetchone())[0]

    async def mark_seen(self, event_id: str) -> bool:
        """Returns True if this is the first time we see event_id."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "INSERT OR IGNORE INTO seen_events (id, created_at) VALUES (?, ?)",
                (event_id, time.time()),
            )
            await db.execute("DELETE FROM seen_events WHERE created_at < ?", (time.time() - 30 * 86400,))
            await db.commit()
            return cur.rowcount == 1

    async def create_pending_send(self, thread_id: str, message: str) -> str:
        token = secrets.token_urlsafe(9)
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO pending_sends (token, thread_id, message, created_at) VALUES (?, ?, ?, ?)",
                (token, thread_id, message, time.time()),
            )
            await db.commit()
        return token

    async def pop_pending_send(self, token: str, max_age_seconds: float = 900) -> tuple[str, str] | None:
        """Atomically consume a pending send (one confirmation = one send)."""
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute(
                "SELECT thread_id, message, created_at FROM pending_sends WHERE token = ?",
                (token,),
            )
            row = await cur.fetchone()
            await db.execute("DELETE FROM pending_sends WHERE token = ?", (token,))
            await db.commit()
        if not row or time.time() - row[2] > max_age_seconds:
            return None
        return row[0], row[1]

    async def get(self, key: str) -> str | None:
        async with aiosqlite.connect(self.path) as db:
            cur = await db.execute("SELECT v FROM kv WHERE k = ?", (key,))
            row = await cur.fetchone()
        return row[0] if row else None

    async def set(self, key: str, value: str) -> None:
        async with aiosqlite.connect(self.path) as db:
            await db.execute(
                "INSERT INTO kv (k, v) VALUES (?, ?) ON CONFLICT(k) DO UPDATE SET v = excluded.v",
                (key, value),
            )
            await db.commit()
