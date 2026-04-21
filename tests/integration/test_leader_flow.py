"""
Integration test: mock Anthropic + mock Docker + mock Slack.
Tests the full graph execution path in mock mode without real API calls.
"""
import os
import pytest

os.environ["AF_MOCK_MODE"] = "true"

from agentforge.core.models import ModelTier, TaskSpec, WorkflowSpec
from agentforge.core.state import make_initial_state
from workflows.builder import GraphBuilder


def make_spec(name: str = "test_workflow") -> WorkflowSpec:
    return WorkflowSpec(
        name=name,
        tasks=[
            TaskSpec(id="t1", title="Task 1", acceptance_criteria=["done"]),
            TaskSpec(id="t2", title="Task 2", depends_on=["t1"], acceptance_criteria=["done"]),
        ],
    )


class TestLeaderFlow:
    @pytest.mark.asyncio
    async def test_full_graph_runs_to_completion(self):
        """Full mock graph: build_dag → dispatch → verify_ci → verify_semantic → finalize."""
        spec = make_spec()
        state = make_initial_state(session_id="integration-1", user_request="test flow")
        state["workflow_spec"] = spec

        graph = GraphBuilder().from_spec(spec, with_checkpointer=False)
        config = {"configurable": {"thread_id": "integration-1"}}
        result = await graph.ainvoke(state, config=config)

        assert result["final_report"] is not None
        assert len(result["final_report"]) > 0

    @pytest.mark.asyncio
    async def test_dag_index_all_completed(self):
        """After a clean run, all tasks should be COMPLETED."""
        from agentforge.core.models import TaskStatus

        spec = make_spec()
        state = make_initial_state(session_id="integration-2", user_request="test dag")
        state["workflow_spec"] = spec

        graph = GraphBuilder().from_spec(spec, with_checkpointer=False)
        config = {"configurable": {"thread_id": "integration-2"}}
        result = await graph.ainvoke(state, config=config)

        for task_id, status in result["dag_index"].items():
            assert status == TaskStatus.COMPLETED, f"{task_id} should be COMPLETED, got {status}"

    @pytest.mark.asyncio
    async def test_single_task_workflow(self):
        """Simplest case: single task, no dependencies."""
        spec = WorkflowSpec(
            name="single",
            tasks=[TaskSpec(id="only", acceptance_criteria=["done"])],
        )
        state = make_initial_state(session_id="integration-3", user_request="simple")
        state["workflow_spec"] = spec

        graph = GraphBuilder().from_spec(spec, with_checkpointer=False)
        config = {"configurable": {"thread_id": "integration-3"}}
        result = await graph.ainvoke(state, config=config)

        assert result["final_report"] is not None

    @pytest.mark.asyncio
    async def test_check_context_node_included(self):
        """context_usage_pct should be set after graph run."""
        spec = make_spec()
        state = make_initial_state(session_id="integration-4", user_request="context test")
        state["workflow_spec"] = spec

        graph = GraphBuilder().from_spec(spec, with_checkpointer=False)
        config = {"configurable": {"thread_id": "integration-4"}}
        result = await graph.ainvoke(state, config=config)

        assert "context_usage_pct" in result
        assert 0.0 <= result["context_usage_pct"] <= 1.0
