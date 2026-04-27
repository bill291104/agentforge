from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from langgraph.graph import END, StateGraph

from agentforge.core.checkpoint import get_checkpointer
from agentforge.core.models import TaskSpec, WorkflowSpec
from agentforge.core.state import AgentForgeState


class CyclicDependencyError(ValueError):
    pass


def _topological_sort(tasks: list[TaskSpec]) -> list[TaskSpec]:
    """Return tasks in topological order; raise CyclicDependencyError if cycle detected."""
    task_map = {t.id: t for t in tasks}
    visited: set[str] = set()
    in_stack: set[str] = set()
    order: list[TaskSpec] = []

    def visit(tid: str) -> None:
        if tid in in_stack:
            raise CyclicDependencyError(f"Cyclic dependency detected at task '{tid}'")
        if tid in visited:
            return
        in_stack.add(tid)
        for dep in task_map[tid].depends_on:
            if dep not in task_map:
                raise ValueError(f"Task '{tid}' depends on unknown task '{dep}'")
            visit(dep)
        in_stack.discard(tid)
        visited.add(tid)
        order.append(task_map[tid])

    for t in tasks:
        visit(t.id)
    return order


class GraphBuilder:
    """
    Converts a WorkflowSpec (from YAML or code) into a compiled LangGraph.

    The graph structure:
      START → refine_requirements → build_dag → dispatch_workers
            ↕ (via check_context)
      dispatch_workers → [worker nodes in parallel] → verify_ci
      verify_ci → verify_semantic | escalate
      verify_semantic → finalize | escalate
      escalate → dispatch_workers | interrupt_l4
      finalize → END
    """

    def from_yaml(self, path: str | Path) -> Any:
        spec = WorkflowSpec.from_yaml(str(path))
        return self.from_spec(spec)

    def from_spec(self, spec: WorkflowSpec, with_checkpointer: bool = True) -> Any:
        _validate_spec(spec)
        graph = StateGraph(AgentForgeState)

        # Core nodes — imported here to avoid circular imports at module load
        from agentforge.graph.nodes import (
            build_dag_node,
            check_context_node,
            compress_context_node,
            dispatch_workers_node,
            escalate_node,
            finalize_node,
            interrupt_l2_node,
            interrupt_l4_node,
            merge_task_node,
            present_plan_node,
            refine_requirements_node,
            spawn_sub_orchestrator_node,
            verify_ci_node,
            verify_semantic_node,
        )
        from agentforge.graph.edges import (
            route_after_escalate,
            route_after_finalize,
            route_after_interrupt_l4,
            route_after_merge_task,
            route_after_present_plan,
            route_after_verify_ci,
            route_after_verify_semantic,
            route_context,
        )

        graph.add_node("refine_requirements", refine_requirements_node)
        graph.add_node("build_dag", build_dag_node)
        graph.add_node("present_plan", present_plan_node)
        graph.add_node("check_context", check_context_node)
        graph.add_node("compress_context", compress_context_node)
        graph.add_node("spawn_sub_orchestrator", spawn_sub_orchestrator_node)
        graph.add_node("dispatch_workers", dispatch_workers_node)
        graph.add_node("verify_ci", verify_ci_node)
        graph.add_node("verify_semantic", verify_semantic_node)
        graph.add_node("merge_task", merge_task_node)
        graph.add_node("escalate", escalate_node)
        graph.add_node("interrupt_l2", interrupt_l2_node)
        graph.add_node("interrupt_l4", interrupt_l4_node)
        graph.add_node("finalize", finalize_node)

        # Fixed edges
        graph.set_entry_point("refine_requirements")
        graph.add_edge("refine_requirements", "build_dag")
        graph.add_edge("build_dag", "present_plan")
        graph.add_edge("compress_context", "dispatch_workers")
        graph.add_edge("spawn_sub_orchestrator", "dispatch_workers")
        graph.add_edge("dispatch_workers", "verify_ci")
        graph.add_edge("interrupt_l2", "dispatch_workers")

        # finalize: if tests passed → END; if tests failed → check_context for re-dispatch
        graph.add_conditional_edges("finalize", route_after_finalize,
                                    {"check_context": "check_context", END: END})

        # interrupt_l4: "continue" → check_context (re-dispatch); "stop" → finalize
        graph.add_conditional_edges("interrupt_l4", route_after_interrupt_l4,
                                    {"check_context": "check_context", "finalize": "finalize"})

        # Conditional edges
        graph.add_conditional_edges("present_plan", route_after_present_plan)
        graph.add_conditional_edges("check_context", route_context)
        graph.add_conditional_edges("verify_ci", route_after_verify_ci)
        graph.add_conditional_edges("verify_semantic", route_after_verify_semantic)
        graph.add_conditional_edges("merge_task", route_after_merge_task)
        graph.add_conditional_edges("escalate", route_after_escalate)

        # Store spec in compiled graph for reference
        # interrupt_l2_node and interrupt_l4_node use interrupt() internally —
        # adding interrupt_before would cause a double-pause (interrupt_before fires,
        # then the node's own interrupt() fires), requiring two Command(resume=...)
        # calls and showing the approval button twice. Rely on interrupt() only.
        compiled = graph.compile(
            checkpointer=get_checkpointer() if with_checkpointer else None,
        )
        compiled._workflow_spec = spec  # type: ignore[attr-defined]
        return compiled


def _validate_spec(spec: WorkflowSpec) -> None:
    """Validate that the spec has no cycles and all dependencies exist."""
    _topological_sort(spec.tasks)
