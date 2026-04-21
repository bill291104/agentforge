import pytest

from agentforge.core.models import ModelTier, TaskSpec, WorkflowSpec
from workflows.builder import CyclicDependencyError, GraphBuilder, _topological_sort


def make_spec(*task_defs) -> WorkflowSpec:
    tasks = [TaskSpec(id=tid, depends_on=list(deps)) for tid, deps in task_defs]
    return WorkflowSpec(name="test", tasks=tasks)


class TestTopologicalSort:
    def test_linear_chain(self):
        spec = make_spec(("a", []), ("b", ["a"]), ("c", ["b"]))
        order = _topological_sort(spec.tasks)
        ids = [t.id for t in order]
        assert ids.index("a") < ids.index("b") < ids.index("c")

    def test_parallel_then_join(self):
        spec = make_spec(("a", []), ("b", []), ("c", ["a", "b"]))
        order = _topological_sort(spec.tasks)
        ids = [t.id for t in order]
        assert ids.index("a") < ids.index("c")
        assert ids.index("b") < ids.index("c")

    def test_cyclic_dependency_raises(self):
        spec = make_spec(("a", ["b"]), ("b", ["a"]))
        with pytest.raises(CyclicDependencyError):
            _topological_sort(spec.tasks)

    def test_unknown_dependency_raises(self):
        spec = make_spec(("a", ["nonexistent"]))
        with pytest.raises(ValueError, match="unknown task"):
            _topological_sort(spec.tasks)

    def test_self_loop_raises(self):
        spec = make_spec(("a", ["a"]))
        with pytest.raises(CyclicDependencyError):
            _topological_sort(spec.tasks)

    def test_empty_tasks(self):
        spec = WorkflowSpec(name="empty")
        assert _topological_sort(spec.tasks) == []


class TestGraphBuilderFromYaml:
    def test_loads_feature_dev_yaml(self):
        builder = GraphBuilder()
        graph = builder.from_yaml("workflows/templates/feature_dev.yaml")
        assert graph is not None

    def test_loads_bug_fix_yaml(self):
        builder = GraphBuilder()
        graph = builder.from_yaml("workflows/templates/bug_fix.yaml")
        assert graph is not None

    def test_yaml_spec_attached(self):
        builder = GraphBuilder()
        graph = builder.from_yaml("workflows/templates/feature_dev.yaml")
        assert graph._workflow_spec.name == "feature_development"

    def test_missing_yaml_raises(self):
        builder = GraphBuilder()
        with pytest.raises(FileNotFoundError):
            builder.from_yaml("workflows/templates/nonexistent.yaml")


class TestGraphBuilderFromSpec:
    def test_compiles_simple_spec(self):
        spec = make_spec(("task1", []), ("task2", ["task1"]))
        builder = GraphBuilder()
        graph = builder.from_spec(spec, with_checkpointer=False)
        assert graph is not None

    def test_cyclic_spec_raises(self):
        spec = make_spec(("a", ["b"]), ("b", ["a"]))
        builder = GraphBuilder()
        with pytest.raises(CyclicDependencyError):
            builder.from_spec(spec, with_checkpointer=False)

    def test_graph_has_expected_nodes(self):
        spec = make_spec(("t1", []))
        builder = GraphBuilder()
        graph = builder.from_spec(spec, with_checkpointer=False)
        node_ids = set(graph.get_graph().nodes.keys())
        for expected in ["refine_requirements", "build_dag", "dispatch_workers",
                         "verify_ci", "verify_semantic", "escalate", "finalize"]:
            assert expected in node_ids, f"Missing node: {expected}"
