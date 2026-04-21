import pytest
from pydantic import ValidationError

from agentforge.core.models import (
    AgentEntry,
    CIResult,
    EscalationAction,
    EscalationLevel,
    ImprovementProposal,
    ModelTier,
    SandboxResult,
    SemanticResult,
    TaskInstruction,
    TaskNode,
    TaskReport,
    TaskSpec,
    TaskStatus,
    WorkflowSpec,
)


def make_instruction(**kwargs) -> TaskInstruction:
    defaults = dict(
        task_id="t1",
        title="Test task",
        description="desc",
        acceptance_criteria=["criterion A"],
        model_tier=ModelTier.HAIKU,
        timeout_minutes=10,
    )
    defaults.update(kwargs)
    return TaskInstruction(**defaults)


class TestTaskInstruction:
    def test_serialization_roundtrip(self):
        inst = make_instruction()
        assert TaskInstruction.model_validate(inst.model_dump()) == inst

    def test_missing_required_fields(self):
        with pytest.raises(ValidationError):
            TaskInstruction(task_id="t1")  # missing many required fields


class TestTaskReport:
    def test_defaults(self):
        report = TaskReport(task_id="t1", status=TaskStatus.COMPLETED, summary="done")
        assert report.tokens_used == 0
        assert report.escalation_history == []

    def test_serialization(self):
        r = TaskReport(task_id="t1", status=TaskStatus.FAILED, summary="err")
        assert TaskReport.model_validate(r.model_dump()) == r


class TestAgentEntry:
    def test_default_status(self):
        entry = AgentEntry(
            agent_id="a1", model_tier=ModelTier.HAIKU,
            model_name="claude-haiku-4-5-20251001",
        )
        assert entry.status == "idle"
        assert entry.success_rate_7d == 1.0


class TestTaskNode:
    def test_default_status(self):
        node = TaskNode(instruction=make_instruction())
        assert node.status == TaskStatus.PENDING
        assert node.escalation_level == EscalationLevel.L0
        assert node.attempt_count == 0


class TestWorkflowSpec:
    def test_empty_tasks(self):
        spec = WorkflowSpec(name="test")
        assert spec.tasks == []

    def test_with_tasks(self):
        spec = WorkflowSpec(
            name="test",
            tasks=[TaskSpec(id="t1", model_tier=ModelTier.SONNET)],
        )
        assert spec.tasks[0].id == "t1"

    def test_serialization(self):
        spec = WorkflowSpec(name="test", tasks=[TaskSpec(id="t1")])
        assert WorkflowSpec.model_validate(spec.model_dump()) == spec


class TestCIResult:
    def test_passed(self):
        r = CIResult(passed=True, auto_verified=["file exists"])
        assert r.failed_criteria == []

    def test_failed(self):
        r = CIResult(passed=False, failed_criteria=["test failed"])
        assert not r.passed


class TestSemanticResult:
    def test_accept(self):
        r = SemanticResult(verdict="ACCEPT", criteria_results={"A": "PASS"})
        assert r.verdict == "ACCEPT"

    def test_reject_requires_no_reason_field(self):
        r = SemanticResult(verdict="REJECT", rejection_reason="bad code")
        assert r.rejection_reason == "bad code"


class TestEscalationAction:
    def test_retry(self):
        a = EscalationAction(action="retry")
        assert a.new_agent_id is None

    def test_upgrade(self):
        a = EscalationAction(action="upgrade", new_model_tier=ModelTier.OPUS)
        assert a.new_model_tier == ModelTier.OPUS


class TestImprovementProposal:
    def test_defaults(self):
        p = ImprovementProposal(
            proposal_id="p001",
            trigger="auto",
            problem="slow",
            root_cause="haiku too weak",
            change_type="config",
        )
        assert p.status == "pending"
        assert not p.restart_required


class TestSandboxResult:
    def test_success(self):
        r = SandboxResult(success=True, stdout="ok")
        assert r.exit_code == 0
