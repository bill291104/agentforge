from __future__ import annotations

import json
import logging
import os

logger = logging.getLogger(__name__)

_MODEL = os.getenv("AF_ORCHESTRATOR_MODEL", "claude-sonnet-4-6")

_SYSTEM = """\
You are a requirements analyst for AgentForge, an AI-powered software development platform.

Your goal is to clarify exactly what the user wants to build before any implementation begins.

Rules:
- Ask ONE focused question per turn — the single most important unknown
- Be brief and professional (this is a Slack conversation)
- Respond in the same language the user is using (Korean or English)
- After 2–5 exchanges, when scope, constraints, and success criteria are clear, declare ready
- Do NOT re-ask about things the user already stated

Always respond with valid JSON in one of these two forms:

When you need more information:
{"status": "clarifying", "message": "<your single focused question>"}

When requirements are sufficiently clear:
{
  "status": "ready",
  "summary": "<concise requirements summary in bullet points>",
  "clarification_points": ["<key point clarified 1>", "<key point clarified 2>"]
}
"""


class ClarifierAgent:
    """
    Drives a multi-turn requirements clarification conversation via the Anthropic API.
    History is a standard messages list: [{"role": "user"|"assistant", "content": str}, ...]
    """

    def __init__(self) -> None:
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic()

    async def next_turn(self, history: list[dict]) -> dict:
        """Given the conversation history, return the next action dict."""
        response = await self._client.messages.create(
            model=_MODEL,
            max_tokens=600,
            system=_SYSTEM,
            messages=history,
        )
        raw = response.content[0].text.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            logger.warning("ClarifierAgent: non-JSON response: %.200s", raw)
            return {"status": "clarifying", "message": raw}
