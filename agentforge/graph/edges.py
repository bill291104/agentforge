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
    dag_index = state.get("dag_index", {})

    logger.info("[route] verify_semantic verdict=%s", verdict)

    if verdict == "ACCEPT":
        # Only finalize when all tasks have a terminal status AND at least one completed.
        # If everything failed/blocked, keep escalating (or let escalate handle it).
        terminal = {TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED}
        all_terminal = all(s in terminal for s in dag_index.values())
        any_completed = any(s == TaskStatus.COMPLETED for s in dag_index.values())

        if not dag_index or (all_terminal and any_completed):
            logger.info("[route] → finalize (completed=%d/%d)",
                        sum(1 for s in dag_index.values() if s == TaskStatus.COMPLETED),
                        len(dag_index))
            return "finalize"

        if all_terminal and not any_completed:
            # Everything failed — escalate so the escalation loop can decide
            logger.info("[route] all tasks failed/blocked, forcing escalate")
            return "escalate"

        logger.info("[route] → check_context (more tasks pending)")
        return "check_context"

    logger.info("[route] → escalate (reason=%.80s)", result.get("rejection_reason", ""))
    return "escalate"


def route_after_escalate(state: AgentForgeState) -> str:
    level = state.get("current_escalation_level", 0)
    dest = "interrupt_l4" if level >= EscalationLevel.L4 else "dispatch_workers"
    logger.info("[route] escalate level=%d → %s", level, dest)
    return dest
