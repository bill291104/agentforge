from __future__ import annotations

from typing import Annotated, Any, Optional

from langgraph.graph.message import add_messages
from typing_extensions import TypedDict

from agentforge.core.models import (
    AgentEntry,
    EscalationLevel,
    TaskNode,
    TaskStatus,
    WorkflowSpec,
)


def make_initial_state(session_id: str, user_request: str) -> "AgentForgeState":
    return AgentForgeState(
        session_id=session_id,
        user_request=user_request,
        workflow_spec=None,
        task_nodes={},
        dag_index={},
        agent_pool=[],
        escalation_history=[],
        current_escalation_level=0,
        failing_task_id=None,
        context_usage_pct=0.0,
        completed_summaries=[],
        ci_passed=True,
        ci_result=None,
        semantic_result=None,
        current_task_id=None,
        delegated_task_ids=[],
        final_report=None,
        messages=[],
    )


class AgentForgeState(TypedDict):
    # Session identity
    session_id: str
    user_request: str

    # Workflow
    workflow_spec: Optional[WorkflowSpec]
    task_nodes: dict[str, TaskNode]       # task_id → TaskNode
    dag_index: dict[str, TaskStatus]      # task_id → status (compact summary)

    # Agent pool
    agent_pool: list[AgentEntry]

    # Escalation tracking
    escalation_history: list[dict[str, Any]]
    current_escalation_level: int         # 현재 처리 중인 태스크의 에스컬레이션 레벨
    failing_task_id: Optional[str]        # 현재 에스컬레이션 대상 태스크

    # Context compression
    context_usage_pct: float
    completed_summaries: list[str]        # 압축된 1줄 요약 목록

    # Verification
    ci_passed: bool
    ci_result: Optional[dict[str, Any]]
    semantic_result: Optional[dict[str, Any]]
    current_task_id: Optional[str]        # 현재 검증 중인 태스크

    # Sub-orchestrator delegation
    delegated_task_ids: list[str]         # 서브 오케스트레이터에 위임된 태스크들

    # Final output
    final_report: Optional[str]

    # LangGraph messages (for agent turns)
    messages: Annotated[list, add_messages]
