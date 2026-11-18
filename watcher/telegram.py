"""Minimal Telegram Bot API client (long polling, no extra dependency)."""

from __future__ import annotations

from typing import Any

import httpx

MAX_LEN = 4000


class Telegram:
    def __init__(self, token: str | None, chat_id: str | None) -> None:
        self.token = token
        self.chat_id = str(chat_id) if chat_id else None
        self.base = f"https://api.telegram.org/bot{token}" if token else None

    @property
    def enabled(self) -> bool:
        return bool(self.base and self.chat_id)

    async def _post(self, method: str, payload: dict[str, Any], timeout: float = 30) -> Any:
        if not self.base:
            return None
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.post(f"{self.base}/{method}", json=payload)
            resp.raise_for_status()
            return resp.json().get("result")

    async def send(self, text: str, buttons: list[list[dict[str, str]]] | None = None) -> None:
        if not self.enabled:
            return
        chunks = [text[i : i + MAX_LEN] for i in range(0, len(text), MAX_LEN)] or [""]
        for i, chunk in enumerate(chunks):
            payload: dict[str, Any] = {
                "chat_id": self.chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }
            if buttons and i == len(chunks) - 1:
                payload["reply_markup"] = {"inline_keyboard": buttons}
            await self._post("sendMessage", payload)

    async def answer_callback(self, callback_id: str, text: str = "") -> None:
        await self._post("answerCallbackQuery", {"callback_query_id": callback_id, "text": text})

    async def get_updates(self, offset: int | None, timeout: int = 50) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout, "allowed_updates": ["message", "callback_query"]}
        if offset is not None:
            payload["offset"] = offset
        return await self._post("getUpdates", payload, timeout=timeout + 10) or []
