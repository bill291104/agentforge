import os
import pytest

os.environ["AF_MOCK_MODE"] = "true"

from agentforge.core.models import (
    ModelTier,
    TaskInstruction,
    TaskNode,
    TaskSpec,
    TaskStatus,
    WorkflowSpec,
)
from agentforge.core.state import AgentForgeState
from agentforge.graph.nodes import (
    _cascade_block,
    _get_ready_task_ids,
    _upgrade_tier,
    build_dag_node,
    check_context_node,
    dispatch_workers_node,
    finalize_node,
)


def make_state(**kwargs) -> AgentForgeState:
    defaults: AgentForgeState = {
        "session_id": "test-session",
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


def make_instruction(task_id: str, depends_on: list[str] = None) -> TaskInstruction:
    return TaskInstruction(
        task_id=task_id,
        title=f"Task {task_id}",
        description="desc",
        acceptance_criteria=["done"],
        model_tier=ModelTier.HAIKU,
        timeout_minutes=5,
        depends_on=depends_on or [],
    )


class TestGetReadyTaskIds:
    def test_no_deps_ready(self):
        inst = make_instruction("t1")
        node = TaskNode(instruction=inst)
        task_nodes = {"t1": node}
        dag_index = {"t1": TaskStatus.PENDING}
        assert _get_ready_task_ids(task_nodes, dag_index) == ["t1"]

    def test_dep_not_done(self):
        node = TaskNode(instruction=make_instruction("t2", ["t1"]))
        task_nodes = {"t2": node}
        dag_index = {"t1": TaskStatus.RUNNING, "t2": TaskStatus.PENDING}
        assert _get_ready_task_ids(task_nodes, dag_index) == []

    def test_dep_completed_ready(self):
        node = TaskNode(instruction=make_instruction("t2", ["t1"]))
        task_nodes = {"t2": node}
        dag_index = {"t1": TaskStatus.COMPLETED, "t2": TaskStatus.PENDING}
        assert _get_ready_task_ids(task_nodes, dag_index) == ["t2"]

    def test_already_running_not_returned(self):
        node = TaskNode(instruction=make_instruction("t1"))
        task_nodes = {"t1": node}
        dag_index = {"t1": TaskStatus.RUNNING}
        assert _get_ready_task_ids(task_nodes, dag_index) == []


class TestCascadeBlock:
    def test_direct_dependent_blocked(self):
        n1 = TaskNode(instruction=make_instruction("t1"))
        n2 = TaskNode(instruction=make_instruction("t2", ["t1"]))
        task_nodes = {"t1": n1, "t2": n2}
        dag_index = {"t1": TaskStatus.FAILED, "t2": TaskStatus.PENDING}
        _cascade_block("t1", task_nodes, dag_index)
        assert dag_index["t2"] == TaskStatus.BLOCKED

    def test_transitive_block(self):
        n1 = TaskNode(instruction=make_instruction("t1"))
        n2 = TaskNode(instruction=make_instruction("t2", ["t1"]))
        n3 = TaskNode(instruction=make_instruction("t3", ["t2"]))
        task_nodes = {"t1": n1, "t2": n2, "t3": n3}
        dag_index = {k: TaskStatus.PENDING for k in ["t1", "t2", "t3"]}
        _cascade_block("t1", task_nodes, dag_index)
        assert dag_index["t2"] == TaskStatus.BLOCKED
        assert dag_index["t3"] == TaskStatus.BLOCKED

    def test_independent_not_blocked(self):
        n1 = TaskNode(instruction=make_instruction("t1"))
        n2 = TaskNode(instruction=make_instruction("t2"))  # no deps
        task_nodes = {"t1": n1, "t2": n2}
        dag_index = {"t1": TaskStatus.FAILED, "t2": TaskStatus.PENDING}
        _cascade_block("t1", task_nodes, dag_index)
        assert dag_index["t2"] == TaskStatus.PENDING


class TestUpgradeTier:
    def test_haiku_to_sonnet(self):
        assert _upgrade_tier(ModelTier.HAIKU) == ModelTier.SONNET

    def test_sonnet_to_opus(self):
        assert _upgrade_tier(ModelTier.SONNET) == ModelTier.OPUS

    def test_opus_stays_opus(self):
        assert _upgrade_tier(ModelTier.OPUS) == ModelTier.OPUS


class TestBuildDagNode:
    @pytest.mark.asyncio
    async def test_creates_task_nodes(self):
        spec = WorkflowSpec(name="test", tasks=[
            TaskSpec(id="t1", acceptance_criteria=["done"]),
            TaskSpec(id="t2", depends_on=["t1"], acceptance_criteria=["done"]),
        ])
        state = make_state(workflow_spec=spec)
        result = await build_dag_node(state)
        assert "t1" in result["task_nodes"]
        assert "t2" in result["task_nodes"]
        assert result["dag_index"]["t1"] == TaskStatus.PENDING
        assert result["dag_index"]["t2"] == TaskStatus.PENDING

    @pytest.mark.asyncio
    async def test_preserves_dependencies(self):
        spec = WorkflowSpec(name="test", tasks=[
            TaskSpec(id="a", acceptance_criteria=["x"]),
            TaskSpec(id="b", depends_on=["a"], acceptance_criteria=["x"]),
        ])
        state = make_state(workflow_spec=spec)
        result = await build_dag_node(state)
        assert result["task_nodes"]["b"].instruction.depends_on == ["a"]


class TestCheckContextNode:
    @pytest.mark.asyncio
    async def test_returns_usage_pct(self):
        state = make_state()
        result = await check_context_node(state)
        assert "context_usage_pct" in result
        assert 0.0 <= result["context_usage_pct"] <= 1.0

    @pytest.mark.asyncio
    async def test_large_context_higher_pct(self):
        # Fill with lots of data
        state_small = make_state()
        state_large = make_state(
            completed_summaries=["summary " * 1000],
        )
        r_small = await check_context_node(state_small)
        r_large = await check_context_node(state_large)
        assert r_large["context_usage_pct"] > r_small["context_usage_pct"]


class TestDispatchWorkersNode:
    @pytest.mark.asyncio
    async def test_runs_ready_tasks(self):
        spec = WorkflowSpec(name="test", tasks=[
            TaskSpec(id="t1", acceptance_criteria=["done"]),
        ])
        dag_result = await build_dag_node(make_state(workflow_spec=spec))
        state = make_state(
            workflow_spec=spec,
            task_nodes=dag_result["task_nodes"],
            dag_index=dag_result["dag_index"],
        )
        result = await dispatch_workers_node(state)
        assert result["dag_index"]["t1"] in (TaskStatus.COMPLETED, TaskStatus.FAILED)

    @pytest.mark.asyncio
    async def test_no_ready_tasks_returns_none(self):
        state = make_state(
            task_nodes={},
            dag_index={},
        )
        result = await dispatch_workers_node(state)
        assert result.get("current_task_id") is None


class TestFinalizeNode:
    @pytest.mark.asyncio
    async def test_generates_report(self):
        state = make_state(
            dag_index={"t1": TaskStatus.COMPLETED, "t2": TaskStatus.COMPLETED},
            completed_summaries=["t1 완료", "t2 완료"],
        )
        result = await finalize_node(state)
        assert "final_report" in result
        assert "완료" in result["final_report"]
