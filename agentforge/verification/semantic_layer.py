from __future__ import annotations

import json
import logging
import os

from agentforge.core.models import (
    CIResult,
    MODEL_IDS,
    ModelTier,
    SemanticResult,
    TaskInstruction,
    TaskReport,
    TaskStatus,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = """\
[VERIFICATION MODE - EFFORT: XHIGH]
당신은 소프트웨어 개발 품질 검증 전문가입니다.
각 수락 기준에 대해 PASS/FAIL과 1문장 근거를 명시하라.
모든 기준을 통과하지 않으면 반드시 REJECT를 선언하라.
응답은 반드시 아래 JSON 형식을 따르라:
{
  "verdict": "ACCEPT" | "REJECT",
  "criteria_results": {"기준명": "PASS" | "FAIL", ...},
  "rejection_reason": "string or null",
  "suggested_fix": "string or null"
}
"""


class SemanticVerifier:
    def __init__(self) -> None:
        self._model = os.getenv("AF_LEADER_MODEL", MODEL_IDS[ModelTier.OPUS])

    async def verify(
        self,
        instruction: TaskInstruction,
        report: TaskReport,
        ci_result: CIResult,
    ) -> SemanticResult:
        if _is_mock():
            criteria_results = {c: "PASS" for c in instruction.acceptance_criteria}
            return SemanticResult(verdict="ACCEPT", criteria_results=criteria_results)

        # Fast-path reject: worker already declared failure
        if report.status in (TaskStatus.FAILED, TaskStatus.TIMEOUT):
            logger.info(
                "Semantic fast-reject: worker status=%s summary=%.120s",
                report.status, report.summary,
            )
            return SemanticResult(
                verdict="REJECT",
                criteria_results={c: "FAIL" for c in instruction.acceptance_criteria},
                rejection_reason=f"워커가 실패를 선언함: {report.summary[:200]}",
                suggested_fix="워커 실패 원인을 분석하고 태스크를 더 작은 단위로 분리하거나 모델 티어를 올리세요.",
            )

        import anthropic

        client = anthropic.AsyncAnthropic()
        prompt = _build_prompt(instruction, report, ci_result)

        logger.debug("Semantic verify prompt:\n%s", prompt[:800])

        response = await client.messages.create(
            model=self._model,
            max_tokens=1024,
            system=_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()
        logger.debug("Semantic raw response: %s", raw[:500])

        result = _parse_response(raw, instruction)
        logger.info(
            "Semantic verdict=%s reason=%.120s",
            result.verdict, result.rejection_reason or "",
        )
        return result


def _build_prompt(
    instruction: TaskInstruction,
    report: TaskReport,
    ci_result: CIResult,
) -> str:
    return (
        f"## 태스크: {instruction.title}\n\n"
        f"### 수락 기준\n"
        + "\n".join(f"- {c}" for c in instruction.acceptance_criteria)
        + f"\n\n### CI 검증 결과\n"
        f"통과: {ci_result.passed}\n"
        f"자동 확인됨: {ci_result.auto_verified}\n"
        f"실패 항목: {ci_result.failed_criteria}\n\n"
        f"### 워커 보고서\n"
        f"상태: {report.status}\n"
        f"요약: {report.summary}\n"
        f"산출물: {report.deliverables}\n"
        f"근거: {json.dumps(report.evidence, ensure_ascii=False)}\n\n"
        "위 정보를 바탕으로 각 수락 기준을 판정하라."
    )


def _parse_response(raw: str, instruction: TaskInstruction) -> SemanticResult:
    text = raw
    try:
        if "```" in text:
            parts = text.split("```")
            for part in parts[1::2]:
                candidate = part.lstrip("json").strip()
                try:
                    data = json.loads(candidate)
                    return SemanticResult.model_validate(data)
                except (json.JSONDecodeError, Exception):
                    continue
            raise ValueError("JSON 블록을 찾을 수 없음")
        data = json.loads(text.strip())
        return SemanticResult.model_validate(data)
    except Exception as exc:
        # Parsing failed → conservative REJECT (not ACCEPT)
        # An unparseable response means we cannot confirm acceptance.
        logger.warning("Semantic parse failed (%s) — defaulting to REJECT. raw=%.200s", exc, raw)
        return SemanticResult(
            verdict="REJECT",
            criteria_results={c: "FAIL" for c in instruction.acceptance_criteria},
            rejection_reason=f"검증 응답 파싱 실패: {exc}",
            suggested_fix="검증을 재시도하거나 수동으로 결과를 확인하세요.",
        )


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"
