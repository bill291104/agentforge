from __future__ import annotations

import asyncio
import logging
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

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


def _build_failure_context(node: TaskNode, state: AgentForgeState) -> str:
    parts = []
    if node.report:
        parts.append(f"**워커 보고**: {node.report.summary[:300]}")
    sem = state.get("semantic_result") or {}
    if sem.get("rejection_reason"):
        parts.append(f"**검증 거부 이유**: {sem['rejection_reason'][:300]}")
    if sem.get("suggested_fix"):
        parts.append(f"**권장 수정**: {sem['suggested_fix'][:300]}")
    return "\n".join(parts) if parts else "이전 시도에서 git_commit이 호출되지 않았거나 수락 기준 미달."


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

    logger.info("[refine] user_request=%.120s", state.get("user_request", ""))

    if _is_mock():
        spec = WorkflowSpec(
            name="mock_workflow",
            tasks=[
                TaskSpec(id="task_1", title="Mock task", model_tier=ModelTier.HAIKU,
                         acceptance_criteria=["done"]),
            ],
        )
        logger.info("[refine] mock workflow=%s tasks=%s", spec.name, [(t.id, str(t.model_tier)) for t in spec.tasks])
        return {"workflow_spec": spec}

    from agentforge.agents.leader import LeaderAgent
    agent = LeaderAgent()
    user_request = state["user_request"]
    spec = await agent.refine_requirements(user_request)

    # If parsing fell back to a skeleton spec, inject user_request as description
    # so the worker has something to work with even in degraded mode.
    if spec.name == "fallback":
        logger.warning("[refine] fallback spec — injecting user_request into task description")
        from agentforge.core.models import TaskSpec
        spec = spec.model_copy(update={
            "tasks": [
                spec.tasks[0].model_copy(update={
                    "description": user_request,
                    "acceptance_criteria": ["요구사항을 구현한 파일 생성", "코드가 실행 가능한 상태"],
                })
            ]
        })

    logger.info(
        "[refine] workflow=%s tasks=%s",
        spec.name,
        [(t.id, str(t.model_tier)) for t in spec.tasks],
    )
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

    logger.info(
        "[build_dag] tasks=%s deps=%s",
        list(task_nodes.keys()),
        {t.id: t.depends_on for t in spec.tasks},
    )

    # Write instruction files to workspace so workers can read them
    workspace_root = state.get("workspace_root", "")
    if workspace_root:
        ws_root_path = Path(workspace_root).resolve()
        if ws_root_path.exists():
            from agentforge.workspace.manager import WorkspaceManager
            ws = WorkspaceManager(ws_root_path.name)
            ws.root = ws_root_path
            for spec_task in spec.tasks:
                node = task_nodes[spec_task.id]
                content = _render_instruction(node.instruction)
                ws.write_instruction(spec_task.id, content)
            sha = ws.commit("chore: write task instructions")
            logger.info(
                "[build_dag] wrote %d instruction file(s) commit=%s",
                len(spec.tasks), sha,
            )

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

    logger.info(
        "[dispatch] dag_index=%s delegated=%s",
        {k: str(v) for k, v in dag_index.items()},
        list(delegated),
    )

    ready_ids = [
        tid for tid in _get_ready_task_ids(task_nodes, dag_index)
        if tid not in delegated
    ]
    workspace_root = state.get("workspace_root", "")
    logger.info("[dispatch] ready_ids=%s workspace_root=%s", ready_ids, workspace_root)

    if not ready_ids:
        # All done or all blocked — pick first failing for escalation target
        logger.warning("[dispatch] no ready tasks — dag_index=%s", {k: str(v) for k, v in dag_index.items()})
        return {"current_task_id": None}

    from agentforge.agents.worker import WorkerAgent

    async def run_task(tid: str) -> tuple[str, TaskReport]:
        node = task_nodes[tid]
        logger.info(
            "[dispatch] starting task=%s model=%s title=%.60s description_len=%d",
            tid, node.instruction.model_tier, node.instruction.title,
            len(node.instruction.description or ""),
        )
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
        logger.info(
            "[dispatch] task=%s status=%s attempt=%d summary=%.100s",
            tid, new_status, node.attempt_count + 1, report.summary,
        )
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
    logger.info("[verify_ci] task=%s", task_id)
    if not task_id:
        logger.info("[verify_ci] no task_id — skipping, ci_passed=True")
        return {"ci_passed": True, "ci_result": {}}

    node = state["task_nodes"].get(task_id)
    logger.info("[verify_ci] task=%s has_report=%s", task_id, bool(node and node.report))
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
    logger.info("[verify_semantic] task=%s ci_passed=%s", task_id, state.get("ci_passed"))
    if not task_id:
        logger.info("[verify_semantic] no task_id — skipping, verdict=ACCEPT")
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

    logger.info(
        "[escalate] task=%s level=%s dag_snapshot=%s",
        task_id, level,
        {k: str(v) for k, v in dag_index.items()},
    )

    if level == EscalationLevel.L0:
        # Retry same agent — enrich instructions with failure context
        new_level = EscalationLevel.L1
        failure_ctx = _build_failure_context(node, state)
        enriched_desc = (
            node.instruction.description
            + f"\n\n---\n## ⚠️ 재시도 1회차 (이전 실패 원인)\n{failure_ctx}"
        )
        new_instruction = node.instruction.model_copy(update={"description": enriched_desc})
        node = node.model_copy(update={
            "escalation_level": new_level,
            "status": TaskStatus.PENDING,
            "instruction": new_instruction,
            "report": None,
        })
        dag_index[task_id] = TaskStatus.PENDING
        logger.info("[escalate] L0→L1: retry same agent task=%s failure_ctx=%.80s", task_id, failure_ctx)
    elif level == EscalationLevel.L1:
        # Spawn new agent of same tier — enrich instructions further
        new_level = EscalationLevel.L2
        failure_ctx = _build_failure_context(node, state)
        enriched_desc = (
            node.instruction.description  # already has 1st retry context
            + f"\n\n## ⚠️ 재시도 2회차 (추가 실패 원인)\n{failure_ctx}"
        )
        new_instruction = node.instruction.model_copy(update={"description": enriched_desc})
        node = node.model_copy(update={
            "escalation_level": new_level,
            "status": TaskStatus.PENDING,
            "instruction": new_instruction,
            "assigned_agent_id": None,
            "report": None,
        })
        dag_index[task_id] = TaskStatus.PENDING
        logger.info("[escalate] L1→L2: spawn new agent task=%s failure_ctx=%.80s", task_id, failure_ctx)
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
        dag_index[task_id] = TaskStatus.PENDING  # reset so dispatch_workers picks it up
        logger.info(
            "[escalate] L2→L3: upgrade tier %s→%s task=%s",
            node.instruction.model_tier, upgraded_tier, task_id,
        )
    elif level == EscalationLevel.L3:
        # Stop task, block dependents, continue rest
        node = node.model_copy(update={"status": TaskStatus.FAILED})
        dag_index[task_id] = TaskStatus.FAILED
        _cascade_block(task_id, task_nodes, dag_index)
        new_level = EscalationLevel.L4
        logger.warning("[escalate] L3→L4: task permanently failed task=%s", task_id)
    else:
        new_level = EscalationLevel.L4
        logger.warning("[escalate] L4+: already at max escalation task=%s", task_id)

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

async def interrupt_l2_node(state: AgentForgeState) -> dict[str, Any]:
    """Pause and request user approval before upgrading model tier (L2 escalation)."""
    from langgraph.types import interrupt

    task_id = state.get("current_task_id", "unknown")
    task_nodes = dict(state.get("task_nodes", {}))
    dag_index  = dict(state.get("dag_index", {}))
    node = task_nodes.get(task_id)
    current_tier = node.instruction.model_tier if node else ModelTier.HAIKU
    next_tier    = _upgrade_tier(current_tier)

    logger.info("[interrupt_l2] task=%s tier=%s→%s", task_id, current_tier, next_tier)

    user_choice = interrupt({
        "type": "l2_approval",
        "task_id": task_id,
        "current_tier": str(current_tier),
        "next_tier": str(next_tier),
    })

    if user_choice.get("choice") == "upgrade":
        new_instruction = node.instruction.model_copy(update={"model_tier": next_tier})
        task_nodes[task_id] = node.model_copy(update={
            "instruction": new_instruction,
            "status": TaskStatus.PENDING,
            "escalation_level": EscalationLevel.L3,
            "report": None,
        })
        dag_index[task_id] = TaskStatus.PENDING
        logger.info("[interrupt_l2] user approved upgrade → %s", next_tier)
        return {
            "task_nodes": task_nodes,
            "dag_index": dag_index,
            "current_escalation_level": int(EscalationLevel.L3),
        }
    else:
        if node:
            task_nodes[task_id] = node.model_copy(update={"status": TaskStatus.FAILED})
        dag_index[task_id] = TaskStatus.FAILED
        logger.info("[interrupt_l2] user declined — task %s permanently failed", task_id)
        return {
            "task_nodes": task_nodes,
            "dag_index": dag_index,
            "current_escalation_level": int(EscalationLevel.L4),
        }


async def interrupt_l4_node(state: AgentForgeState) -> dict[str, Any]:
    """Pause for user intervention before ending the graph at L4 escalation."""
    from langgraph.types import interrupt

    task_id = state.get("current_task_id", "unknown")
    task_nodes = state.get("task_nodes", {})
    dag_index = state.get("dag_index", {})

    blocked = [tid for tid, s in dag_index.items() if s == TaskStatus.BLOCKED]
    still_runnable = list(_get_ready_task_ids(task_nodes, dag_index))

    report = (
        f"⚠️ L4 에스컬레이션: `{task_id}` 태스크가 자동 해결 불가\n"
        f"블록된 태스크: {blocked}\n"
        f"계속 진행 가능한 태스크: {still_runnable}\n\n"
        "진행 방법을 선택해주세요: [재시도] [중단]"
    )

    # Pause here and wait for user choice via Command(resume="continue"|"stop")
    interrupt({
        "type": "l4_approval",
        "task_id": task_id,
        "report": report,
    })

    return {"final_report": report}


# ---------------------------------------------------------------------------
# Node: present_plan
# ---------------------------------------------------------------------------

async def present_plan_node(state: AgentForgeState) -> dict[str, Any]:
    """Present the generated plan to the user and wait for approval."""
    from langgraph.types import interrupt

    task_nodes = state.get("task_nodes", {})

    lines = ["## 📋 작업 계획서\n", f"총 {len(task_nodes)}개 태스크\n"]
    for tid, node in task_nodes.items():
        inst = node.instruction
        tier = str(inst.model_tier).split(".")[-1].capitalize()
        deps = ", ".join(f"`{d}`" for d in inst.depends_on) or "없음"
        criteria = "\n".join(f"  - {c}" for c in inst.acceptance_criteria)
        lines += [
            f"### `{tid}` — {inst.title}",
            f"모델: {tier} | 타임아웃: {inst.timeout_minutes}분 | 의존: {deps}",
            (inst.description or "")[:300],
            f"수락 기준:\n{criteria}",
            "",
        ]
    plan_md = "\n".join(lines)

    ws_root = state.get("workspace_root", "")
    if ws_root:
        ws_path = Path(ws_root)
        if ws_path.exists():
            from agentforge.workspace.manager import WorkspaceManager
            ws = WorkspaceManager(ws_path.name)
            ws.root = ws_path
            (ws.root / "PLAN.md").write_text(plan_md, encoding="utf-8")
            ws.commit("docs: write project plan")

    logger.info("[present_plan] plan ready — interrupting for approval")
    user_choice = interrupt({"type": "plan_approval", "plan": plan_md})

    if isinstance(user_choice, dict) and user_choice.get("action") == "modify":
        feedback = user_choice.get("feedback", "")
        logger.info("[present_plan] user requested modification: %.80s", feedback)
        updated_request = state.get("user_request", "") + f"\n\n수정 요청: {feedback}"
        return {
            "user_request": updated_request,
            "task_nodes": {},
            "dag_index": {},
            "workflow_spec": None,
        }

    logger.info("[present_plan] user approved plan")
    return {}


# ---------------------------------------------------------------------------
# Node: merge_task
# ---------------------------------------------------------------------------

async def merge_task_node(state: AgentForgeState) -> dict[str, Any]:
    """Merge the completed task branch into main and update PLAN.md."""
    task_id = state.get("current_task_id")
    ws_root = state.get("workspace_root", "")

    if not (task_id and ws_root):
        return {}

    ws_path = Path(ws_root)
    from agentforge.workspace.manager import WorkspaceManager
    ws = WorkspaceManager(ws_path.name)
    ws.root = ws_path

    sha = ""
    try:
        sha = ws.merge_branch(f"task/{task_id}", into="main")
        logger.info("[merge_task] merged task/%s → main sha=%s", task_id, sha)
    except Exception as exc:
        logger.warning("[merge_task] merge failed for task/%s: %s", task_id, exc)

    plan_path = ws.root / "PLAN.md"
    if plan_path.exists():
        try:
            content = plan_path.read_text(encoding="utf-8")
            sha_tag = f" (merge: {sha[:7]})" if sha else ""
            content = content.replace(
                f"### `{task_id}`",
                f"### ✅ `{task_id}`{sha_tag}",
            )
            plan_path.write_text(content, encoding="utf-8")
            ws.commit(f"docs: {task_id} complete")
        except Exception as exc:
            logger.warning("[merge_task] PLAN.md update failed: %s", exc)

    node = state.get("task_nodes", {}).get(task_id)
    summary = (node.report.summary[:100] if (node and node.report) else "")
    summaries = list(state.get("completed_summaries", [])) + [f"[{task_id}] {summary}"]
    return {"completed_summaries": summaries}


# ---------------------------------------------------------------------------
# Node: finalize
# ---------------------------------------------------------------------------

async def finalize_node(state: AgentForgeState) -> dict[str, Any]:
    """Generate completion report."""
    task_nodes = state.get("task_nodes", {})
    dag_index = state.get("dag_index", {})
    summaries = state.get("completed_summaries", [])
    escalations = state.get("escalation_history", [])
    workspace_root = state.get("workspace_root", "")

    completed = sum(1 for s in dag_index.values() if s == TaskStatus.COMPLETED)
    total = len(dag_index)
    failed = sum(1 for s in dag_index.values() if s == TaskStatus.FAILED)
    retries = sum(
        n.attempt_count - 1 for n in task_nodes.values() if n.attempt_count > 1
    )
    tokens = sum(
        n.report.tokens_used for n in task_nodes.values() if n.report and n.report.tokens_used
    )
    duration = sum(
        n.report.duration_seconds for n in task_nodes.values() if n.report and n.report.duration_seconds
    )

    ws = None
    git_log = ""
    if workspace_root:
        from agentforge.workspace.manager import WorkspaceManager
        ws = WorkspaceManager(Path(workspace_root).name)
        ws.root = Path(workspace_root)
        git_log = ws.git_log(n=10)

    run_guide = _detect_run_guide(ws)

    report = "\n".join(filter(None, [
        "## 🎉 작업 완료 보고서",
        f"완료: {completed}/{total} | 실패: {failed} | 재시도: {retries}회 | 토큰: {tokens:,} | 소요: {duration:.0f}초",
        "",
        "### 산출물" if summaries else "",
        *[f"- {s}" for s in summaries],
        "",
        "### 실행 방법",
        run_guide,
        "",
        "### 최근 커밋 (main)",
        f"```\n{git_log}\n```" if git_log else "",
    ]))

    if ws:
        try:
            (ws.root / "FINAL_REPORT.md").write_text(report, encoding="utf-8")
            ws.commit("docs: final report")
        except Exception as exc:
            logger.warning("[finalize] FINAL_REPORT.md write failed: %s", exc)

    return {"final_report": report}


def _detect_run_guide(ws) -> str:
    """Detect how to run the project from file existence — no LLM needed."""
    if ws is None:
        return "(워크스페이스 없음)"
    checks = [
        (ws.root / "package.json",       "```\nnpm install && npm run dev\n```\n→ http://localhost:3000"),
        (ws.root / "requirements.txt",   "```\npip install -r requirements.txt\npython main.py\n```"),
        (ws.root / "pyproject.toml",     "```\nuv run python main.py\n```"),
        (ws.root / "Makefile",           "```\nmake run\n```"),
        (ws.root / "docker-compose.yml", "```\ndocker compose up\n```"),
    ]
    for path, guide in checks:
        if path.exists():
            return guide
    return "(README.md 또는 워크스페이스를 확인하세요)"


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


def _render_instruction(instruction: TaskInstruction) -> str:
    """Render a task instruction as markdown for the worker to read."""
    criteria = "\n".join(f"- {c}" for c in instruction.acceptance_criteria)
    return (
        f"# Task: {instruction.task_id}\n"
        f"**Branch**: task/{instruction.task_id}\n"
        f"**Model tier**: {instruction.model_tier}\n"
        f"**Timeout**: {instruction.timeout_minutes}분\n\n"
        f"## 설명\n{instruction.description}\n\n"
        f"## 필요 입력\n{instruction.inputs or '(없음)'}\n\n"
        f"## 수락 기준\n{criteria}\n"
    )


def _upgrade_tier(tier: ModelTier) -> ModelTier:
    order = [ModelTier.HAIKU, ModelTier.SONNET, ModelTier.OPUS]
    try:
        idx = order.index(tier)
        return order[min(idx + 1, len(order) - 1)]
    except ValueError:
        return ModelTier.OPUS
