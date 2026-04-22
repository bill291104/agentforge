from __future__ import annotations

import asyncio
import os
import uuid
from datetime import UTC, datetime
from typing import Any

from agentforge.core.models import (
    EscalationLevel,
    ModelTier,
    TaskInstruction,
    TaskNode,
    TaskReport,
    TaskStatus,
    WorkflowSpec,
)
from agentforge.core.registry import AgentRegistry
from agentforge.core.state import AgentForgeState


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_registry(state: AgentForgeState) -> AgentRegistry:
    """Reconstruct registry from state's agent_pool snapshot."""
    from agentforge.core.models import AgentEntry
    registry = AgentRegistry()
    for entry_data in state.get("agent_pool", []):
        if isinstance(entry_data, dict):
            registry.register(AgentEntry.model_validate(entry_data))
        else:
            registry.register(entry_data)
    return registry


def _is_mock() -> bool:
    return os.getenv("AF_MOCK_MODE", "false").lower() == "true"


# ---------------------------------------------------------------------------
# Node: refine_requirements
# ---------------------------------------------------------------------------

async def refine_requirements_node(state: AgentForgeState) -> dict[str, Any]:
    """
    Opus analyses the user request and selects/creates a WorkflowSpec.
    In mock mode returns a minimal spec directly.
    """
    from agentforge.core.registry import AgentRegistry
    from agentforge.core.models import TaskSpec

    if _is_mock():
        spec = WorkflowSpec(
            name="mock_workflow",
            tasks=[
                TaskSpec(id="task_1", title="Mock task", model_tier=ModelTier.HAIKU,
                         acceptance_criteria=["done"]),
            ],
        )
        return {"workflow_spec": spec}

    from agentforge.agents.leader import LeaderAgent
    agent = LeaderAgent()
    spec = await agent.refine_requirements(state["user_request"])
    return {"workflow_spec": spec}


# ---------------------------------------------------------------------------
# Node: build_dag
# ---------------------------------------------------------------------------

async def build_dag_node(state: AgentForgeState) -> dict[str, Any]:
    """Convert WorkflowSpec into TaskNode map and initial dag_index."""
    spec: WorkflowSpec = state["workflow_spec"]
    task_nodes: dict[str, TaskNode] = {}
    dag_index: dict[str, TaskStatus] = {}

    for task_spec in spec.tasks:
        instruction = TaskInstruction(
            task_id=task_spec.id,
            title=task_spec.title,
            description=task_spec.description,
            acceptance_criteria=task_spec.acceptance_criteria,
            model_tier=task_spec.model_tier,
            timeout_minutes=task_spec.timeout_minutes,
            retry_limit=task_spec.retry_limit,
            priority=task_spec.priority,
            depends_on=task_spec.depends_on,
            deliverable_format=task_spec.deliverable_format,
        )
        node = TaskNode(instruction=instruction)
        task_nodes[task_spec.id] = node
        dag_index[task_spec.id] = TaskStatus.PENDING

    return {"task_nodes": task_nodes, "dag_index": dag_index}


# ---------------------------------------------------------------------------
# Node: check_context
# ---------------------------------------------------------------------------

async def check_context_node(state: AgentForgeState) -> dict[str, Any]:
    """Measure approximate context usage and return updated pct."""
    import json
    # Rough token estimate: json-encode the relevant state and count chars/4
    payload = json.dumps({
        "task_nodes": {k: v.model_dump() if hasattr(v, "model_dump") else str(v)
                       for k, v in state.get("task_nodes", {}).items()},
        "completed_summaries": state.get("completed_summaries", []),
        "escalation_history": state.get("escalation_history", []),
    }, default=str)
    estimated_tokens = len(payload) / 4
    max_tokens = 180_000  # Opus context window
    usage_pct = min(estimated_tokens / max_tokens, 1.0)
    return {"context_usage_pct": usage_pct}


# ---------------------------------------------------------------------------
# Node: compress_context
# ---------------------------------------------------------------------------

async def compress_context_node(state: AgentForgeState) -> dict[str, Any]:
    """
    Replace completed task reports with 1-line summaries.
    In mock mode, generate summaries without API call.
    """
    task_nodes = dict(state.get("task_nodes", {}))
    summaries = list(state.get("completed_summaries", []))

    for tid, node in task_nodes.items():
        if node.status == TaskStatus.COMPLETED and node.report:
            if _is_mock():
                summary = f"[{tid}] completed: {node.report.summary}"
            else:
                from agentforge.agents.worker import WorkerAgent
                summary = await WorkerAgent.summarize(node.report)
            summaries.append(summary)
            # Clear the detailed report to free context
            compressed_node = node.model_copy(update={"report": None})
            task_nodes[tid] = compressed_node

    return {"task_nodes": task_nodes, "completed_summaries": summaries}


# ---------------------------------------------------------------------------
# Node: spawn_sub_orchestrator
# ---------------------------------------------------------------------------

async def spawn_sub_orchestrator_node(state: AgentForgeState) -> dict[str, Any]:
    """
    Delegate independent sub-DAG tasks to a Sonnet sub-orchestrator.
    Returns updated state with delegated_task_ids.
    """
    task_nodes = dict(state.get("task_nodes", {}))
    dag_index = dict(state.get("dag_index", {}))

    # Find PENDING tasks with all dependencies completed
    ready_ids = _get_ready_task_ids(task_nodes, dag_index)
    if not ready_ids:
        return {}

    delegated = list(state.get("delegated_task_ids", []))
    # Delegate a subset to sub-orchestrator (leave at least one for leader)
    to_delegate = ready_ids[1:] if len(ready_ids) > 1 else []
    delegated.extend(to_delegate)

    return {"delegated_task_ids": delegated}


# ---------------------------------------------------------------------------
# Node: dispatch_workers
# ---------------------------------------------------------------------------

async def dispatch_workers_node(state: AgentForgeState) -> dict[str, Any]:
    """
    Launch ready tasks in parallel. Returns updated task_nodes and dag_index.
    """
    task_nodes = dict(state.get("task_nodes", {}))
    dag_index = dict(state.get("dag_index", {}))
    delegated = set(state.get("delegated_task_ids", []))

    ready_ids = [
        tid for tid in _get_ready_task_ids(task_nodes, dag_index)
        if tid not in delegated
    ]

    if not ready_ids:
        # All done or all blocked — pick first failing for escalation target
        return {"current_task_id": None}

    from agentforge.agents.worker import WorkerAgent

    async def run_task(tid: str) -> tuple[str, TaskReport]:
        node = task_nodes[tid]
        node = node.model_copy(update={
            "status": TaskStatus.RUNNING,
            "started_at": datetime.now(UTC),
        })
        task_nodes[tid] = node
        dag_index[tid] = TaskStatus.RUNNING

        worker = WorkerAgent(model_tier=node.instruction.model_tier)
        report = await worker.execute(
            node.instruction,
            workspace_root=state.get("workspace_root"),
        )
        return tid, report

    results = await asyncio.gather(*[run_task(tid) for tid in ready_ids])

    # Persist results
    completed_task_id = None
    for tid, report in results:
        node = task_nodes[tid]
        new_status = report.status
        node = node.model_copy(update={
            "status": new_status,
            "report": report,
            "completed_at": datetime.now(UTC),
            "attempt_count": node.attempt_count + 1,
        })
        task_nodes[tid] = node
        dag_index[tid] = new_status
        if new_status in (TaskStatus.COMPLETED, TaskStatus.FAILED):
            completed_task_id = tid  # last one for verify step

    # Mark downstream tasks BLOCKED if any failed
    for tid, report in results:
        if report.status == TaskStatus.FAILED:
            _cascade_block(tid, task_nodes, dag_index)

    return {
        "task_nodes": task_nodes,
        "dag_index": dag_index,
        "current_task_id": completed_task_id,
    }


# ---------------------------------------------------------------------------
# Node: verify_ci
# ---------------------------------------------------------------------------

async def verify_ci_node(state: AgentForgeState) -> dict[str, Any]:
    """Run CI (mechanical) verification on the last completed task."""
    task_id = state.get("current_task_id")
    if not task_id:
        return {"ci_passed": True, "ci_result": {}}

    node = state["task_nodes"].get(task_id)
    if not node or not node.report:
        return {"ci_passed": True, "ci_result": {}}

    from agentforge.verification.ci_layer import CIVerifier
    verifier = CIVerifier()
    result = await verifier.verify(
        node.instruction, node.report,
        workspace_root=state.get("workspace_root"),
    )

    return {
        "ci_passed": result.passed,
        "ci_result": result.model_dump(),
        "current_task_id": task_id,
    }


# ---------------------------------------------------------------------------
# Node: verify_semantic
# ---------------------------------------------------------------------------

async def verify_semantic_node(state: AgentForgeState) -> dict[str, Any]:
    """Run Opus semantic verification."""
    task_id = state.get("current_task_id")
    if not task_id:
        return {"semantic_result": {"verdict": "ACCEPT"}}

    node = state["task_nodes"].get(task_id)
    if not node or not node.report:
        return {"semantic_result": {"verdict": "ACCEPT"}}

    from agentforge.core.models import CIResult
    ci_result = CIResult.model_validate(state.get("ci_result", {"passed": True}))

    from agentforge.verification.semantic_layer import SemanticVerifier
    verifier = SemanticVerifier()
    result = await verifier.verify(node.instruction, node.report, ci_result)

    return {"semantic_result": result.model_dump()}


# ---------------------------------------------------------------------------
# Node: escalate
# ---------------------------------------------------------------------------

async def escalate_node(state: AgentForgeState) -> dict[str, Any]:
    """Handle L0~L3 escalation automatically."""
    task_id = state.get("current_task_id")
    task_nodes = dict(state.get("task_nodes", {}))
    dag_index = dict(state.get("dag_index", {}))
    history = list(state.get("escalation_history", []))

    if not task_id or task_id not in task_nodes:
        return {"current_escalation_level": EscalationLevel.L4}

    node = task_nodes[task_id]
    level = node.escalation_level

    if level == EscalationLevel.L0:
        # Retry same agent with rejection notice
        new_level = EscalationLevel.L1
        node = node.model_copy(update={
            "escalation_level": new_level,
            "status": TaskStatus.PENDING,
            "report": None,
        })
    elif level == EscalationLevel.L1:
        # Spawn new agent of same tier
        new_level = EscalationLevel.L2
        node = node.model_copy(update={
            "escalation_level": new_level,
            "status": TaskStatus.PENDING,
            "assigned_agent_id": None,
            "report": None,
        })
    elif level == EscalationLevel.L2:
        # Upgrade model tier
        upgraded_tier = _upgrade_tier(node.instruction.model_tier)
        new_instruction = node.instruction.model_copy(update={"model_tier": upgraded_tier})
        node = node.model_copy(update={
            "escalation_level": EscalationLevel.L3,
            "instruction": new_instruction,
            "status": TaskStatus.PENDING,
            "report": None,
        })
        new_level = EscalationLevel.L3
    elif level == EscalationLevel.L3:
        # Stop task, block dependents, continue rest
        node = node.model_copy(update={"status": TaskStatus.FAILED})
        dag_index[task_id] = TaskStatus.FAILED
        _cascade_block(task_id, task_nodes, dag_index)
        new_level = EscalationLevel.L4
    else:
        new_level = EscalationLevel.L4

    task_nodes[task_id] = node
    history.append({
        "task_id": task_id,
        "level": int(level),
        "timestamp": datetime.now(UTC).isoformat(),
    })

    return {
        "task_nodes": task_nodes,
        "dag_index": dag_index,
        "escalation_history": history,
        "current_escalation_level": int(new_level),
    }


# ---------------------------------------------------------------------------
# Node: interrupt_l4
# ---------------------------------------------------------------------------

async def interrupt_l4_node(state: AgentForgeState) -> dict[str, Any]:
    """Generate L4 user report. Actual interrupt is handled by LangGraph."""
    task_id = state.get("current_task_id", "unknown")
    task_nodes = state.get("task_nodes", {})
    dag_index = state.get("dag_index", {})

    blocked = [tid for tid, s in dag_index.items() if s == TaskStatus.BLOCKED]
    still_runnable = [
        tid for tid in _get_ready_task_ids(task_nodes, dag_index)
    ]

    report = (
        f"⚠️ L4 에스컬레이션: `{task_id}` 태스크가 자동 해결 불가\n"
        f"블록된 태스크: {blocked}\n"
        f"계속 진행 가능한 태스크: {still_runnable}\n\n"
        "진행 방법을 선택해주세요: [재시도] [중단]"
    )
    return {"final_report": report}


# ---------------------------------------------------------------------------
# Node: finalize
# ---------------------------------------------------------------------------

async def finalize_node(state: AgentForgeState) -> dict[str, Any]:
    """Generate completion report."""
    dag_index = state.get("dag_index", {})
    summaries = state.get("completed_summaries", [])
    escalations = state.get("escalation_history", [])
    workspace_root = state.get("workspace_root", "")

    completed = sum(1 for s in dag_index.values() if s == TaskStatus.COMPLETED)
    total = len(dag_index)
    failed = sum(1 for s in dag_index.values() if s == TaskStatus.FAILED)

    lines = [
        f"작업 완료",
        f"완료: {completed}/{total}  실패: {failed}  에스컬레이션: {len(escalations)}회",
    ]
    if summaries:
        lines += ["", "산출물 요약:"] + [f"  - {s}" for s in summaries]
    if workspace_root:
        from agentforge.workspace.manager import WorkspaceManager
        from pathlib import Path
        ws = WorkspaceManager(Path(workspace_root).name)
        ws.root = Path(workspace_root)
        git_log = ws.git_log(n=5)
        if git_log:
            lines += ["", f"작업 디렉토리: `{workspace_root}`", "최근 커밋:", f"```\n{git_log}\n```"]

    return {"final_report": "\n".join(lines)}


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _get_ready_task_ids(
    task_nodes: dict[str, TaskNode],
    dag_index: dict[str, TaskStatus],
) -> list[str]:
    """Return PENDING task IDs whose all dependencies are COMPLETED."""
    ready = []
    for tid, node in task_nodes.items():
        if dag_index.get(tid) != TaskStatus.PENDING:
            continue
        deps_done = all(
            dag_index.get(dep) == TaskStatus.COMPLETED
            for dep in node.instruction.depends_on
        )
        if deps_done:
            ready.append(tid)
    return sorted(ready, key=lambda tid: task_nodes[tid].instruction.priority)


def _cascade_block(
    failed_id: str,
    task_nodes: dict[str, TaskNode],
    dag_index: dict[str, TaskStatus],
) -> None:
    """Mark all tasks that transitively depend on failed_id as BLOCKED."""
    for tid, node in task_nodes.items():
        if failed_id in node.instruction.depends_on:
            if dag_index.get(tid) == TaskStatus.PENDING:
                dag_index[tid] = TaskStatus.BLOCKED
                task_nodes[tid] = node.model_copy(update={"status": TaskStatus.BLOCKED})
                _cascade_block(tid, task_nodes, dag_index)


def _upgrade_tier(tier: ModelTier) -> ModelTier:
    order = [ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS]
    try:
        idx = order.index(tier)
        return order[min(idx + 1, len(order) - 1)]
    except ValueError:
        return ModelTier.OPUS
