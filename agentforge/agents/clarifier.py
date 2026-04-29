from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_MODEL = os.getenv("AF_ORCHESTRATOR_MODEL", "claude-sonnet-4-6")

_SYSTEM = """\
You are a requirements analyst for AgentForge, an AI-powered software development platform.

Your goal is to clarify exactly what the user wants to build before any implementation begins.

## File system access
You have access to the local file system via tools. When the user provides a local path:
1. Use list_local_directory to understand the project structure FIRST
2. Read key files (README, package.json, requirements.txt, main source files) with read_local_file
3. Use this context to ask smarter, more targeted clarifying questions
Do NOT say you cannot access local paths — you can.

## Rules
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

_CLARIFIER_TOOLS = [
    {
        "name": "read_local_file",
        "description": "로컬 파일을 읽습니다. 사용자가 경로를 제공한 경우 프로젝트 파일을 직접 읽어 요구사항 파악에 활용하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "읽을 파일의 절대 경로 (예: C:/aether-j/README.md)"},
                "max_lines": {"type": "integer", "description": "반환할 최대 줄 수 (기본: 200)"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "list_local_directory",
        "description": "로컬 디렉토리 구조를 확인합니다. 사용자가 경로를 제공한 경우 프로젝트 구조를 먼저 파악하세요.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "조회할 디렉토리의 절대 경로"},
                "recursive": {"type": "boolean", "description": "하위 디렉토리 포함 여부 (기본: false)"},
            },
            "required": ["path"],
        },
    },
]


def _execute_tool(name: str, args: dict) -> str:
    """ClarifierAgent 내부 도구 실행 (동기). 파일 크기가 작아 sync로 처리."""
    try:
        if name == "read_local_file":
            path = Path(args.get("path", ""))
            max_lines = int(args.get("max_lines", 200))
            if not path.exists():
                return f"파일 없음: {path}"
            if not path.is_file():
                return f"파일이 아님: {path}"
            text = path.read_text(encoding="utf-8", errors="replace")
            lines = text.splitlines()
            if len(lines) > max_lines:
                return "\n".join(lines[:max_lines]) + f"\n... ({len(lines)}줄 중 {max_lines}줄)"
            return text

        if name == "list_local_directory":
            path = Path(args.get("path", ""))
            recursive = bool(args.get("recursive", False))
            if not path.exists():
                return f"경로 없음: {path}"
            if not path.is_dir():
                return f"디렉토리가 아님: {path}"
            entries: list[str] = []
            if recursive:
                import os as _os
                for root, dirs, files in _os.walk(path):
                    dirs[:] = [d for d in sorted(dirs) if not d.startswith(".")]
                    rp = Path(root)
                    for d in dirs:
                        entries.append(f"[D] {(rp / d).relative_to(path)}/")
                    for f in sorted(files):
                        if not f.startswith("."):
                            entries.append(f"[F] {(rp / f).relative_to(path)}")
                    if len(entries) > 200:
                        entries.append("... (생략)")
                        break
            else:
                for item in sorted(path.iterdir(), key=lambda p: (p.is_file(), p.name)):
                    if item.name.startswith("."):
                        continue
                    entries.append(f"[{'F' if item.is_file() else 'D'}] {item.name}{'/' if item.is_dir() else ''}")
            return f"{path} ({len(entries)}개):\n" + "\n".join(entries)

        return f"알 수 없는 도구: {name}"
    except Exception as exc:
        return f"도구 실행 오류: {exc}"


class ClarifierAgent:
    """
    Drives a multi-turn requirements clarification conversation via the Anthropic API.
    Supports local file system tools so users can share local project paths directly.
    History is a standard messages list: [{"role": "user"|"assistant", "content": str}, ...]
    """

    def __init__(self) -> None:
        from anthropic import AsyncAnthropic
        self._client = AsyncAnthropic()

    async def next_turn(self, history: list[dict]) -> dict:
        """Given the conversation history, return the next action dict."""
        # Internal messages may include tool call turns (not persisted in external history)
        internal: list[dict] = list(history)

        for _turn in range(8):  # allow up to 7 tool-calling turns before text response
            response = await self._client.messages.create(
                model=_MODEL,
                max_tokens=1000,
                system=_SYSTEM,
                messages=internal,
                tools=_CLARIFIER_TOOLS,
            )

            # Handle tool use internally — not added to external history
            if response.stop_reason == "tool_use":
                tool_results: list[dict[str, Any]] = []
                for block in response.content:
                    if getattr(block, "type", None) == "tool_use":
                        result = _execute_tool(block.name, block.input or {})
                        logger.info("[clarifier] tool=%s path=%s result_len=%d",
                                    block.name, (block.input or {}).get("path", ""), len(result))
                        tool_results.append({
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": result,
                        })
                internal.append({"role": "assistant", "content": response.content})
                internal.append({"role": "user", "content": tool_results})
                continue

            # Text response — find and parse JSON
            text_block = next(
                (b for b in response.content if getattr(b, "type", None) == "text"), None
            )
            raw = text_block.text.strip() if text_block else ""

            # 1) 전체가 코드펜스인 경우 벗겨내기
            if raw.startswith("```"):
                lines = raw.split("\n")
                raw = "\n".join(lines[1:-1]) if len(lines) > 2 else raw

            # 2) 순수 JSON 파싱 시도
            try:
                return json.loads(raw)
            except json.JSONDecodeError:
                pass

            # 3) 혼합 응답(텍스트 + JSON 코드블록)에서 JSON 추출
            import re as _re
            json_match = _re.search(r"```(?:json)?\s*(\{.*?\})\s*```", raw, _re.DOTALL)
            if json_match:
                try:
                    parsed = json.loads(json_match.group(1))
                    return parsed
                except json.JSONDecodeError:
                    pass

            # 4) 폴백: JSON 블록 제거 후 텍스트만 메시지로 반환
            clean = _re.sub(r"```(?:json)?\s*\{.*?\}\s*```", "", raw, flags=_re.DOTALL).strip()
            logger.warning("ClarifierAgent: non-JSON response: %.200s", raw)
            return {"status": "clarifying", "message": clean or raw}

        logger.error("[clarifier] max tool-call turns exceeded")
        return {"status": "clarifying", "message": "요구사항을 파악하는 데 시간이 걸리고 있습니다. 조금 더 구체적으로 설명해 주실 수 있나요?"}
