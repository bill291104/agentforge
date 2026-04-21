from __future__ import annotations

import asyncio
import json
import os
import time

import anthropic

from agentforge.agents.base import BaseAgent
from agentforge.core.models import (
    MODEL_IDS,
    ModelTier,
    TaskInstruction,
    TaskReport,
    TaskStatus,
)


class WorkerAgent(BaseAgent):
    def __init__(self, model_tier: ModelTier = ModelTier.HAIKU) -> None:
        super().__init__(model_tier)
        self._model = os.getenv(
            "AF_WORKER_MODEL", MODEL_IDS.get(model_tier, MODEL_IDS[ModelTier.HAIKU])
        )

    async def execute(self, instruction: TaskInstruction) -> TaskReport:
        if _is_mock():
            return _mock_report(instruction)

        start = time.monotonic()
        prompt = self._build_worker_prompt(instruction)
        client = anthropic.AsyncAnthropic()

        try:
            response = await self._run_with_timeout(
                client.messages.create(
                    model=self._model,
                    max_tokens=4096,
                    system=_WORKER_SYSTEM_PROMPT,
                    messages=[{"role": "user", "content": prompt}],
                ),
                instruction.timeout_minutes,
            )
        except TimeoutError:
            return TaskReport(
                task_id=instruction.task_id,
                status=TaskStatus.TIMEOUT,
                summary=f"태스크 타임아웃 ({instruction.timeout_minutes}분)",
                tokens_used=0,
                duration_seconds=time.monotonic() - start,
            )

        duration = time.monotonic() - start
        raw = response.content[0].text
        return _parse_report(instruction, raw, response.usage.input_tokens + response.usage.output_tokens, duration)

    def _build_worker_prompt(self, instruction: TaskInstruction) -> str:
        criteria_str = "\n".join(f"- {c}" for c in instruction.acceptance_criteria)
        return (
            f"## 태스크: {instruction.title}\n\n"
            f"### 설명\n{instruction.description}\n\n"
            f"### 필요 입력\n{instruction.inputs}\n\n"
            f"### 수락 기준 (반드시 모두 충족)\n{criteria_str}\n\n"
            f"### 산출물 형식\n{json.dumps(instruction.deliverable_format, ensure_ascii=False)}\n\n"
            "위 태스크를 수행하고 아래 JSON 형식으로 보고서를 제출하라:\n"
            '{"status": "completed"|"failed", "deliverables": [...], '
            '"evidence": {...}, "summary": "1~3문장"}'
        )

    async def _run_with_timeout(self, coro, timeout_minutes: int):
        return await asyncio.wait_for(coro, timeout=timeout_minutes * 60)

    @staticmethod
    async def summarize(report: TaskReport) -> str:
        """Summarize a completed report to 1 line using Haiku."""
        if _is_mock():
            return f"[{report.task_id}] {report.summary}"

        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=MODEL_IDS[ModelTier.HAIKU],
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": f"다음 보고서를 한 문장으로 요약: {report.summary} / 산출물: {report.deliverables}",
            }],
        )
        return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_WORKER_SYSTEM_PROMPT = """\
당신은 소프트웨어 개발 워커 에이전트입니다.
주어진 태스크를 수행하고 JSON 형식의 보고서를 반환하라.
내부 처리 과정은 보고서에 포함하지 않는다.
수락 기준을 모두 충족했을 때만 status: completed를 선언하라.
"""


def _parse_report(instruction: TaskInstruction, raw: str, tokens: int, duration: float) -> TaskReport:
    try:
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        return TaskReport(
            task_id=instruction.task_id,
            status=TaskStatus(data.get("status", "completed")),
            deliverables=data.get("deliverables", []),
            evidence=data.get("evidence", {}),
            summary=data.get("summary", ""),
            tokens_used=tokens,
            duration_seconds=duration,
        )
    except Exception as e:
        return TaskReport(
            task_id=instruction.task_id,
            status=TaskStatus.FAILED,
            summary=f"보고서 파싱 실패: {e}",
            tokens_used=tokens,
            duration_seconds=duration,
        )


def _mock_report(instruction: TaskInstruction) -> TaskReport:
    return TaskReport(
        task_id=instruction.task_id,
        status=TaskStatus.COMPLETED,
        deliverables=[f"mock_{instruction.task_id}.py"],
        evidence={"tests_passed": 1, "tests_failed": 0},
        summary=f"[Mock] {instruction.title} 완료",
        tokens_used=100,
        duration_seconds=0.1,
    )


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"
