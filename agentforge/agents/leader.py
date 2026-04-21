from __future__ import annotations

import json
import os

import anthropic

from agentforge.agents.base import BaseAgent
from agentforge.core.models import MODEL_IDS, ModelTier, TaskSpec, WorkflowSpec

_LEADER_SYSTEM_PROMPT = """\
당신은 소프트웨어 개발 팀의 리더 에이전트입니다.

역할: PM + Tech Lead + QA Manager를 통합한 최상위 오케스트레이터.

원칙:
1. 사용자 요구사항을 상세하고 기술적인 명세로 재정의한다.
2. 태스크를 DAG로 분해하고 각 태스크에 적절한 모델 등급을 배정한다.
3. 하위 에이전트에게는 작업 지시서만 전달한다. 내부 컨텍스트를 공유하지 않는다.
4. 모호성이 있으면 스스로 합리적 가정을 하고 가정 목록을 포함하라.

금지사항:
- 판단이 어렵다고 사용자에게 묻지 않는다.
- 수락 기준 없이 태스크를 완료 처리하지 않는다.
"""


class LeaderAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(ModelTier.OPUS)
        self._model = os.getenv("AF_LEADER_MODEL", MODEL_IDS[ModelTier.OPUS])

    async def refine_requirements(self, user_request: str) -> WorkflowSpec:
        """Analyse the request and return a WorkflowSpec."""
        if _is_mock():
            return WorkflowSpec(
                name="mock_feature",
                tasks=[
                    TaskSpec(id="design", title="설계", model_tier=ModelTier.SONNET,
                             acceptance_criteria=["설계 문서 생성"]),
                    TaskSpec(id="implement", title="구현", model_tier=ModelTier.HAIKU,
                             depends_on=["design"], acceptance_criteria=["구현 완료"]),
                ],
            )

        client = anthropic.AsyncAnthropic()
        prompt = (
            f"다음 요구사항을 분석하여 워크플로우 명세를 JSON으로 반환하라:\n\n{user_request}\n\n"
            "응답 형식:\n"
            '{"name": "workflow_name", "tasks": ['
            '{"id": "task_id", "title": "...", "description": "...", '
            '"model_tier": "haiku|sonnet|opus", "timeout_minutes": 30, '
            '"depends_on": [], "acceptance_criteria": ["..."]}'
            "]}"
        )
        response = await client.messages.create(
            model=self._model,
            max_tokens=2048,
            system=_LEADER_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": prompt}],
        )
        return _parse_workflow_spec(response.content[0].text)

    async def verify_semantic(self, *args, **kwargs):
        """Delegated to SemanticVerifier — kept here for interface symmetry."""
        from agentforge.verification.semantic_layer import SemanticVerifier
        return await SemanticVerifier().verify(*args, **kwargs)


def _parse_workflow_spec(raw: str) -> WorkflowSpec:
    try:
        if "```" in raw:
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        data = json.loads(raw.strip())
        return WorkflowSpec.model_validate(data)
    except Exception:
        # Fallback single-task spec
        return WorkflowSpec(
            name="fallback",
            tasks=[TaskSpec(id="task_1", title="구현", acceptance_criteria=["완료"])],
        )


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"
