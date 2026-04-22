from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

import anthropic

from agentforge.agents.base import BaseAgent
from agentforge.core.models import (
    MODEL_IDS,
    ModelTier,
    TaskInstruction,
    TaskReport,
    TaskStatus,
)

logger = logging.getLogger(__name__)


class WorkerAgent(BaseAgent):
    def __init__(self, model_tier: ModelTier = ModelTier.HAIKU) -> None:
        super().__init__(model_tier)
        self._model = os.getenv(
            "AF_WORKER_MODEL", MODEL_IDS.get(model_tier, MODEL_IDS[ModelTier.HAIKU])
        )

    async def execute(
        self,
        instruction: TaskInstruction,
        workspace_root: str | None = None,
    ) -> TaskReport:
        if _is_mock():
            return _mock_report(instruction, workspace_root)

        start = time.monotonic()
        prompt = self._build_worker_prompt(instruction)
        client = anthropic.AsyncAnthropic()

        try:
            response = await self._run_with_timeout(
                client.messages.create(
                    model=self._model,
                    max_tokens=8192,
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
        tokens = response.usage.input_tokens + response.usage.output_tokens
        raw = response.content[0].text
        report = _parse_report(instruction, raw, tokens, duration)

        # Write files to workspace and commit
        if workspace_root and report.status == TaskStatus.COMPLETED:
            _persist_files(report, workspace_root, instruction.task_id)

        return report

    def _build_worker_prompt(self, instruction: TaskInstruction) -> str:
        criteria_str = "\n".join(f"- {c}" for c in instruction.acceptance_criteria)
        return (
            f"## 태스크: {instruction.title}\n\n"
            f"### 설명\n{instruction.description}\n\n"
            f"### 필요 입력\n{instruction.inputs}\n\n"
            f"### 수락 기준 (반드시 모두 충족)\n{criteria_str}\n\n"
            f"### 산출물 형식\n{json.dumps(instruction.deliverable_format, ensure_ascii=False)}\n\n"
            "위 태스크를 수행하고 아래 JSON 형식으로 보고서를 제출하라.\n"
            "files 배열에 생성하는 모든 파일의 경로와 전체 내용을 포함해야 한다.\n\n"
            "```json\n"
            "{\n"
            '  "status": "completed" | "failed",\n'
            '  "files": [\n'
            '    {"path": "src/app.py", "content": "...전체 파일 내용..."},\n'
            '    {"path": "tests/test_app.py", "content": "..."}\n'
            "  ],\n"
            '  "evidence": {"tests_passed": 0, "tests_failed": 0},\n'
            '  "summary": "1~3문장 요약"\n'
            "}\n"
            "```"
        )

    async def _run_with_timeout(self, coro, timeout_minutes: int):
        return await asyncio.wait_for(coro, timeout=timeout_minutes * 60)

    @staticmethod
    async def summarize(report: TaskReport) -> str:
        if _is_mock():
            return f"[{report.task_id}] {report.summary}"
        client = anthropic.AsyncAnthropic()
        response = await client.messages.create(
            model=MODEL_IDS[ModelTier.HAIKU],
            max_tokens=128,
            messages=[{
                "role": "user",
                "content": (
                    f"다음 보고서를 한 문장으로 요약: {report.summary} "
                    f"/ 산출물: {report.deliverables}"
                ),
            }],
        )
        return response.content[0].text.strip()


# ---------------------------------------------------------------------------
# File persistence
# ---------------------------------------------------------------------------

def _persist_files(report: TaskReport, workspace_root: str, task_id: str) -> None:
    """Write files from report to workspace and commit."""
    from agentforge.workspace.manager import WorkspaceManager

    ws_root = Path(workspace_root)
    # Reconstruct manager without re-initializing
    session_id = ws_root.name
    ws = WorkspaceManager(session_id)
    ws.root = ws_root

    if not report.files:
        logger.debug("No files to persist for task %s", task_id)
        return

    written = ws.write_files(report.files)
    if written:
        sha = ws.commit(f"feat({task_id}): {report.summary[:60]}")
        logger.info("Persisted %d file(s) for %s — commit %s", len(written), task_id, sha)
        report.deliverables = [str(Path(p).relative_to(ws_root)) for p in written]


# ---------------------------------------------------------------------------
# Report parsing
# ---------------------------------------------------------------------------

def _parse_report(
    instruction: TaskInstruction, raw: str, tokens: int, duration: float
) -> TaskReport:
    try:
        # Strip markdown code fences
        text = raw.strip()
        if "```" in text:
            parts = text.split("```")
            # Find the JSON block (skip "json" language tag)
            for part in parts[1::2]:
                candidate = part.lstrip("json").strip()
                try:
                    data = json.loads(candidate)
                    break
                except json.JSONDecodeError:
                    continue
            else:
                raise ValueError("No valid JSON block found")
        else:
            data = json.loads(text)

        files: list[dict] = data.get("files", [])
        # Backwards compat: if old format uses "deliverables" as list of strings
        deliverables = [f["path"] for f in files if isinstance(f, dict) and "path" in f]
        if not deliverables and "deliverables" in data:
            deliverables = data["deliverables"]

        return TaskReport(
            task_id=instruction.task_id,
            status=TaskStatus(data.get("status", "completed")),
            files=files,
            deliverables=deliverables,
            evidence=data.get("evidence", {}),
            summary=data.get("summary", ""),
            tokens_used=tokens,
            duration_seconds=duration,
        )
    except Exception as exc:
        return TaskReport(
            task_id=instruction.task_id,
            status=TaskStatus.FAILED,
            summary=f"보고서 파싱 실패: {exc} | raw={raw[:200]}",
            tokens_used=tokens,
            duration_seconds=duration,
        )


# ---------------------------------------------------------------------------
# Mock
# ---------------------------------------------------------------------------

def _mock_report(instruction: TaskInstruction, workspace_root: str | None) -> TaskReport:
    files = [
        {
            "path": f"src/{instruction.task_id}.py",
            "content": f"# Mock implementation of {instruction.title}\nprint('hello')\n",
        },
        {
            "path": f"tests/test_{instruction.task_id}.py",
            "content": (
                f"# Mock tests for {instruction.task_id}\n"
                "def test_placeholder():\n    assert True\n"
            ),
        },
    ]
    report = TaskReport(
        task_id=instruction.task_id,
        status=TaskStatus.COMPLETED,
        files=files,
        deliverables=[f["path"] for f in files],
        evidence={"tests_passed": 1, "tests_failed": 0},
        summary=f"[Mock] {instruction.title} 완료",
        tokens_used=100,
        duration_seconds=0.1,
    )
    if workspace_root:
        _persist_files(report, workspace_root, instruction.task_id)
    return report


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"
