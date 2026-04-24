from __future__ import annotations

import logging

from agentforge.core.models import EscalationLevel, TaskStatus
from agentforge.core.state import AgentForgeState

logger = logging.getLogger(__name__)


def route_context(state: AgentForgeState) -> str:
    pct = state.get("context_usage_pct", 0.0)
    if pct >= 0.90:
        return "spawn_sub_orchestrator"
    if pct >= 0.70:
        return "compress_context"
    return "dispatch_workers"


def route_after_verify_ci(state: AgentForgeState) -> str:
    passed = state.get("ci_passed", True)
    task_id = state.get("current_task_id", "?")
    dest = "verify_semantic" if passed else "escalate"
    logger.info("[route] verify_ci task=%s ci_passed=%s → %s", task_id, passed, dest)
    return dest


def route_after_verify_semantic(state: AgentForgeState) -> str:
    result  = state.get("semantic_result", {})
    verdict = result.get("verdict", "REJECT")

    logger.info("[route] verify_semantic verdict=%s", verdict)

    if verdict == "ACCEPT":
        logger.info("[route] → merge_task")
        return "merge_task"

    logger.info("[route] → escalate (reason=%.80s)", result.get("rejection_reason", ""))
    return "escalate"


def route_after_merge_task(state: AgentForgeState) -> str:
    dag_index = state.get("dag_index", {})
    terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}
    all_terminal = all(s in terminal for s in dag_index.values())
    any_completed = any(s == TaskStatus.COMPLETED for s in dag_index.values())

    if not dag_index or (all_terminal and any_completed):
        logger.info("[route] merge_task → finalize (completed=%d/%d)",
                    sum(1 for s in dag_index.values() if s == TaskStatus.COMPLETED),
                    len(dag_index))
        return "finalize"

    if all_terminal and not any_completed:
        logger.info("[route] merge_task → escalate (all failed/blocked)")
        return "escalate"

    logger.info("[route] merge_task → check_context (more tasks pending)")
    return "check_context"


def route_after_present_plan(state: AgentForgeState) -> str:
    """After plan approval: if task_nodes cleared (modify request), re-plan; else continue."""
    if not state.get("task_nodes"):
        logger.info("[route] present_plan → refine_requirements (user requested modification)")
        return "refine_requirements"
    logger.info("[route] present_plan → check_context (plan approved)")
    return "check_context"


def route_after_escalate(state: AgentForgeState) -> str:
    level = state.get("current_escalation_level", 0)
    if level >= EscalationLevel.L4:
        dest = "interrupt_l4"
    elif level == EscalationLevel.L2:
        dest = "interrupt_l2"   # 2회 재시도 소진 → 사용자 승인 요청
    else:
        dest = "dispatch_workers"   # L0→L1, L1→L2(retry), L3(approved upgrade)
    logger.info("[route] escalate level=%d → %s", level, dest)
    return dest
