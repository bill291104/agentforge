import os
import pytest

os.environ["AF_MOCK_MODE"] = "true"

from agentforge.core.models import (
    EscalationLevel,
    ModelTier,
    TaskInstruction,
    TaskNode,
    TaskStatus,
)
from agentforge.core.state import AgentForgeState
from agentforge.graph.edges import (
    route_after_escalate,
    route_after_verify_ci,
    route_after_verify_semantic,
    route_context,
)
from agentforge.graph.nodes import escalate_node


def make_state(**kwargs) -> AgentForgeState:
    defaults: AgentForgeState = {
        "session_id": "s",
        "user_request": "test",
        "workflow_spec": None,
        "task_nodes": {},
        "dag_index": {},
        "agent_pool": [],
        "escalation_history": [],
        "current_escalation_level": 0,
        "failing_task_id": None,
        "context_usage_pct": 0.0,
        "completed_summaries": [],
        "ci_passed": True,
        "ci_result": None,
        "semantic_result": None,
        "current_task_id": None,
        "delegated_task_ids": [],
        "final_report": None,
        "messages": [],
    }
    defaults.update(kwargs)
    return defaults


def make_node(task_id: str = "t1", level: EscalationLevel = EscalationLevel.L0,
              model_tier: ModelTier = ModelTier.HAIKU) -> TaskNode:
    inst = TaskInstruction(
        task_id=task_id, title="Task", description="desc",
        acceptance_criteria=["done"], model_tier=model_tier, timeout_minutes=5,
    )
    return TaskNode(instruction=inst, escalation_level=level)


# ---------------------------------------------------------------------------
# Edge routing tests
# ---------------------------------------------------------------------------

class TestRouteContext:
    def test_below_70_dispatches(self):
        assert route_context(make_state(context_usage_pct=0.5)) == "dispatch_workers"

    def test_between_70_90_compresses(self):
        assert route_context(make_state(context_usage_pct=0.75)) == "compress_context"

    def test_above_90_spawns(self):
        assert route_context(make_state(context_usage_pct=0.95)) == "spawn_sub_orchestrator"

    def test_exactly_70_compresses(self):
        assert route_context(make_state(context_usage_pct=0.70)) == "compress_context"

    def test_exactly_90_spawns(self):
        assert route_context(make_state(context_usage_pct=0.90)) == "spawn_sub_orchestrator"


class TestRouteAfterVerifyCI:
    def test_ci_pass_goes_semantic(self):
        assert route_after_verify_ci(make_state(ci_passed=True)) == "verify_semantic"

    def test_ci_fail_escalates(self):
        assert route_after_verify_ci(make_state(ci_passed=False)) == "escalate"


class TestRouteAfterVerifySemantic:
    def test_accept_all_done_finalizes(self):
        state = make_state(
            semantic_result={"verdict": "ACCEPT"},
            dag_index={"t1": TaskStatus.COMPLETED},
        )
        assert route_after_verify_semantic(state) == "finalize"

    def test_accept_more_tasks_checks_context(self):
        state = make_state(
            semantic_result={"verdict": "ACCEPT"},
            dag_index={"t1": TaskStatus.COMPLETED, "t2": TaskStatus.PENDING},
        )
        assert route_after_verify_semantic(state) == "check_context"

    def test_reject_escalates(self):
        state = make_state(semantic_result={"verdict": "REJECT"})
        assert route_after_verify_semantic(state) == "escalate"


class TestRouteAfterEscalate:
    def test_l0_retries(self):
        assert route_after_escalate(make_state(current_escalation_level=0)) == "dispatch_workers"

    def test_l1_retries(self):
        assert route_after_escalate(make_state(current_escalation_level=1)) == "dispatch_workers"

    def test_l2_requests_user_approval(self):
        # L2 = 2 retries exhausted → ask user before upgrading model
        assert route_after_escalate(make_state(current_escalation_level=2)) == "interrupt_l2"

    def test_l3_retries_independent(self):
        # L3 = user approved upgrade, model upgraded → route to dispatch
        assert route_after_escalate(make_state(current_escalation_level=3)) == "dispatch_workers"

    def test_l4_interrupts(self):
        assert route_after_escalate(make_state(current_escalation_level=4)) == "interrupt_l4"


# ---------------------------------------------------------------------------
# Escalate node tests
# ---------------------------------------------------------------------------

class TestEscalateNode:
    @pytest.mark.asyncio
    async def test_l0_increments_to_l1(self):
        node = make_node(level=EscalationLevel.L0)
        state = make_state(
            task_nodes={"t1": node},
            dag_index={"t1": TaskStatus.FAILED},
            current_task_id="t1",
        )
        result = await escalate_node(state)
        updated_node = result["task_nodes"]["t1"]
        assert updated_node.escalation_level == EscalationLevel.L1
        assert updated_node.status == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_l1_increments_to_l2(self):
        node = make_node(level=EscalationLevel.L1)
        state = make_state(
            task_nodes={"t1": node},
            dag_index={"t1": TaskStatus.FAILED},
            current_task_id="t1",
        )
        result = await escalate_node(state)
        assert result["task_nodes"]["t1"].escalation_level == EscalationLevel.L2

    @pytest.mark.asyncio
    async def test_l2_upgrades_model(self):
        node = make_node(level=EscalationLevel.L2, model_tier=ModelTier.HAIKU)
        state = make_state(
            task_nodes={"t1": node},
            dag_index={"t1": TaskStatus.FAILED},
            current_task_id="t1",
        )
        result = await escalate_node(state)
        upgraded = result["task_nodes"]["t1"]
        assert upgraded.instruction.model_tier == ModelTier.SONNET  # upgraded from haiku

    @pytest.mark.asyncio
    async def test_l3_marks_task_failed_and_blocks_deps(self):
        n1 = make_node("t1", EscalationLevel.L3)
        n2 = TaskNode(instruction=TaskInstruction(
            task_id="t2", title="T2", description="",
            acceptance_criteria=["done"], model_tier=ModelTier.HAIKU,
            timeout_minutes=5, depends_on=["t1"],
        ))
        state = make_state(
            task_nodes={"t1": n1, "t2": n2},
            dag_index={"t1": TaskStatus.FAILED, "t2": TaskStatus.PENDING},
            current_task_id="t1",
        )
        result = await escalate_node(state)
        assert result["dag_index"]["t2"] == TaskStatus.BLOCKED
        assert result["current_escalation_level"] == EscalationLevel.L4

    @pytest.mark.asyncio
    async def test_escalation_logged_to_history(self):
        node = make_node(level=EscalationLevel.L0)
        state = make_state(
            task_nodes={"t1": node},
            dag_index={"t1": TaskStatus.FAILED},
            current_task_id="t1",
            escalation_history=[],
        )
        result = await escalate_node(state)
        assert len(result["escalation_history"]) == 1
        assert result["escalation_history"][0]["task_id"] == "t1"
