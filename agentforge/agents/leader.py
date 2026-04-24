from __future__ import annotations

import logging
import os

import anthropic

_logger = logging.getLogger(__name__)

from agentforge.agents.base import BaseAgent
from agentforge.core.models import MODEL_IDS, ModelTier, TaskSpec, WorkflowSpec

_LEADER_SYSTEM_PROMPT = """\
당신은 소프트웨어 개발 팀의 리더 에이전트입니다.

역할: PM + Tech Lead + QA Manager를 통합한 최상위 오케스트레이터.

원칙:
1. 사용자 요구사항을 상세하고 기술적인 명세로 재정의한다.
2. 태스크를 DAG로 분해하고 각 태스크에 적절한 모델 등급을 배정한다.
3. 하위 에이전트에게는 작업 지시서만 전달한다. 내부 컨텍스트를 공유하지 않는다.
4. 모호성이 있으면 스스로 합리적 가정을 하고 태스크 description에 포함하라.
5. 각 태스크를 add_task로 하나씩 추가하고, 모두 완료 후 submit_plan을 호출한다.
6. 단일 호출로 전체 계획을 제출하지 않는다 — 반드시 태스크별로 분리한다.
7. 의존관계(depends_on)를 명확히 설정해 병렬 실행 가능한 태스크를 분리한다.

금지사항:
- 판단이 어렵다고 사용자에게 묻지 않는다.
- 수락 기준 없이 태스크를 완료 처리하지 않는다.
- add_task 없이 submit_plan을 호출하지 않는다.
"""

_ADD_TASK_TOOL = {
    "name": "add_task",
    "description": "계획에 태스크 하나를 추가합니다. 태스크마다 한 번씩 호출하세요.",
    "input_schema": {
        "type": "object",
        "properties": {
            "id": {
                "type": "string",
                "description": "snake_case 고유 식별자 (예: t1_setup, t2_auth)",
            },
            "title": {"type": "string"},
            "description": {
                "type": "string",
                "description": "워커 에이전트가 읽는 상세 구현 지침. 200자 이상 권장.",
            },
            "model_tier": {
                "type": "string",
                "enum": ["haiku", "sonnet", "opus"],
                "description": "haiku=단순 파일 생성, sonnet=로직 구현, opus=복잡한 설계",
            },
            "timeout_minutes": {
                "type": "integer",
                "description": "태스크 최대 실행 시간(분). 기본 30.",
            },
            "depends_on": {
                "type": "array",
                "items": {"type": "string"},
                "description": "이 태스크 실행 전 완료되어야 하는 태스크 ID 목록",
            },
            "acceptance_criteria": {
                "type": "array",
                "items": {"type": "string"},
                "minItems": 1,
                "description": "완료 판정 기준 (최소 1개 필수). 기계적으로 확인 가능한 기준 우선.",
            },
        },
        "required": ["id", "title", "description", "acceptance_criteria"],
    },
}

_SUBMIT_PLAN_TOOL = {
    "name": "submit_plan",
    "description": (
        "모든 태스크 추가 완료 후 계획을 최종 제출합니다. "
        "add_task를 최소 1회 이상 호출한 뒤에만 사용하세요."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "workflow_name": {
                "type": "string",
                "description": "snake_case 워크플로우 식별자 (예: todo_web_app)",
            },
        },
        "required": ["workflow_name"],
    },
}


class LeaderAgent(BaseAgent):
    def __init__(self) -> None:
        super().__init__(ModelTier.OPUS)
        self._model = os.getenv("AF_LEADER_MODEL", MODEL_IDS[ModelTier.OPUS])

    async def refine_requirements(self, user_request: str) -> WorkflowSpec:
        """Analyse the request and return a WorkflowSpec via add_task/submit_plan tool loop."""
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
        pending_tasks: list[TaskSpec] = []
        workflow_name = "workflow"
        messages = [{
            "role": "user",
            "content": (
                "아래 요구사항을 분석해 구현 태스크로 분해하라.\n"
                "각 태스크마다 add_task를 한 번씩 호출하고, 모두 추가한 뒤 submit_plan을 호출하라.\n\n"
                f"요구사항:\n{user_request}"
            ),
        }]

        for turn in range(25):
            response = await client.messages.create(
                model=self._model,
                max_tokens=4096,
                system=[{
                    "type": "text",
                    "text": _LEADER_SYSTEM_PROMPT,
                    "cache_control": {"type": "ephemeral"},
                }],
                messages=messages,
                tools=[_ADD_TASK_TOOL, _SUBMIT_PLAN_TOOL],
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_results = []
            submitted = False

            for block in response.content:
                if block.type != "tool_use":
                    continue

                if block.name == "add_task":
                    try:
                        spec = TaskSpec.model_validate(block.input)
                        # Reject duplicate IDs
                        if any(t.id == spec.id for t in pending_tasks):
                            result = f"❌ 중복 ID: {spec.id} — 다른 ID를 사용하세요"
                        else:
                            pending_tasks.append(spec)
                            result = f"✅ 태스크 추가됨: {spec.id} (현재 {len(pending_tasks)}개)"
                            _logger.info("[plan] add_task: %s", spec.id)
                    except Exception as exc:
                        result = f"❌ 태스크 추가 실패: {exc}"

                elif block.name == "submit_plan":
                    if not pending_tasks:
                        result = "❌ 태스크가 없습니다. 먼저 add_task를 1회 이상 호출하세요."
                    else:
                        workflow_name = block.input.get("workflow_name", "workflow")
                        submitted = True
                        result = f"✅ 계획 제출됨: {workflow_name} ({len(pending_tasks)}개 태스크)"
                        _logger.info("[refine] submit_plan: workflow=%s tasks=%d",
                                     workflow_name, len(pending_tasks))
                else:
                    result = f"알 수 없는 툴: {block.name}"

                tool_results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": result,
                })

            if tool_results:
                messages.append({"role": "user", "content": tool_results})

            if submitted:
                return WorkflowSpec(name=workflow_name, tasks=pending_tasks)

            if response.stop_reason == "end_turn":
                _logger.warning("[refine] end_turn without submit_plan at turn=%d", turn)
                break

        # Fallback: collected tasks without submit_plan
        if pending_tasks:
            _logger.warning("[refine] submit_plan 미호출 — %d개 태스크로 강제 제출", len(pending_tasks))
            return WorkflowSpec(name=workflow_name, tasks=pending_tasks)

        # Last resort: single task
        _logger.error("[refine] 태스크 0개 — 단일 태스크 fallback 사용")
        return WorkflowSpec(
            name="fallback",
            tasks=[TaskSpec(
                id="task_1",
                title="구현",
                description=user_request,
                acceptance_criteria=["요구사항을 구현한 파일 생성", "코드가 실행 가능한 상태"],
            )],
        )

    async def verify_semantic(self, *args, **kwargs):
        """Delegated to SemanticVerifier — kept here for interface symmetry."""
        from agentforge.verification.semantic_layer import SemanticVerifier
        return await SemanticVerifier().verify(*args, **kwargs)

    async def dispatch_user_message(
        self,
        user_message: str,
        thread_state: dict,
        allow_actions: bool,
        post_fn,
        on_start_new_task=None,
        on_retry_session=None,
        on_resume_session=None,
        on_continue_clarification=None,
        on_delete_thread=None,
    ) -> None:
        """Route a user message through multi-turn tool calling."""
        from agentforge.agents.leader_tools import LeaderToolExecutor
        executor = LeaderToolExecutor(
            thread_state=thread_state,
            on_start_new_task=on_start_new_task,
            on_retry_session=on_retry_session,
            on_resume_session=on_resume_session,
            on_continue_clarification=on_continue_clarification,
            on_delete_thread=on_delete_thread,
        )
        await executor.dispatch(user_message, allow_actions=allow_actions, post_fn=post_fn)


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"
