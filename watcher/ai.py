"""Free-tier Gemini helpers: summarise a thread and draft replies (never sends)."""

from __future__ import annotations

import asyncio
import logging

log = logging.getLogger("watcher.ai")

SYSTEM = (
    "You help the user triage their LinkedIn direct messages. Be concise. "
    "Never invent facts about the user. Drafts are suggestions the user will edit."
)


class Drafter:
    def __init__(self, api_key: str | None, model: str = "gemini-2.5-flash") -> None:
        self.api_key = api_key
        self.model = model

    async def _generate(self, prompt: str) -> str:
        if not self.api_key:
            return ""
        from google import genai
        from google.genai import types

        def run() -> str:
            client = genai.Client(api_key=self.api_key)
            resp = client.models.generate_content(
                model=self.model,
                contents=prompt,
                config=types.GenerateContentConfig(system_instruction=SYSTEM, temperature=0.4),
            )
            return (resp.text or "").strip()

        try:
            return await asyncio.to_thread(run)
        except Exception as exc:  # free tier quota etc. — degrade gracefully
            log.warning("Gemini call failed: %s", type(exc).__name__)
            return ""

    async def triage(self, conversation_text: str, instructions: str = "") -> str:
        extra = f"\nExtra instructions from the user: {instructions}" if instructions else ""
        prompt = (
            "Here is a LinkedIn conversation (most recent messages last):\n\n"
            f"{conversation_text[-6000:]}\n\n"
            "1) Summarise the latest incoming message in 1-2 sentences and say what is asked.\n"
            "2) Propose 2-3 short reply drafts (numbered, each under 60 words) in the user's "
            f"voice: one positive, one polite decline/deferral, one asking a clarifying question.{extra}"
        )
        return await self._generate(prompt)
