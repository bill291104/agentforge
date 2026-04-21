from __future__ import annotations

from agentforge.core.models import EscalationLevel, TaskStatus
from agentforge.core.state import AgentForgeState


def route_context(state: AgentForgeState) -> str:
    pct = state.get("context_usage_pct", 0.0)
    if pct >= 0.90:
        return "spawn_sub_orchestrator"
    if pct >= 0.70:
        return "compress_context"
    return "dispatch_workers"


def route_after_verify_ci(state: AgentForgeState) -> str:
    if state.get("ci_passed", True):
        return "verify_semantic"
    return "escalate"


def route_after_verify_semantic(state: AgentForgeState) -> str:
    result = state.get("semantic_result", {})
    if result.get("verdict") == "ACCEPT":
        # Check if all tasks are done
        dag_index = state.get("dag_index", {})
        all_done = all(
            s in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.BLOCKED)
            for s in dag_index.values()
        )
        if all_done or not dag_index:
            return "finalize"
        return "check_context"
    return "escalate"


def route_after_escalate(state: AgentForgeState) -> str:
    level = state.get("current_escalation_level", 0)
    if level >= EscalationLevel.L4:
        return "interrupt_l4"
    return "dispatch_workers"
