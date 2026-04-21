import os
import pytest

os.environ["AF_MOCK_MODE"] = "true"

from agentforge.core.models import (
    CIResult,
    ModelTier,
    TaskInstruction,
    TaskReport,
    TaskStatus,
)
from agentforge.verification.ci_layer import CIVerifier
from agentforge.verification.semantic_layer import SemanticVerifier


def make_instruction(criteria: list[str] = None) -> TaskInstruction:
    return TaskInstruction(
        task_id="t1",
        title="Test",
        description="desc",
        acceptance_criteria=criteria or ["done"],
        model_tier=ModelTier.HAIKU,
        timeout_minutes=5,
    )


def make_report(deliverables: list[str] = None, evidence: dict = None, status=TaskStatus.COMPLETED) -> TaskReport:
    return TaskReport(
        task_id="t1",
        status=status,
        deliverables=deliverables or [],
        evidence=evidence or {},
        summary="done",
    )


class TestCIVerifier:
    @pytest.mark.asyncio
    async def test_no_deliverables_passes(self):
        """No deliverables specified → no file-existence check."""
        verifier = CIVerifier()
        result = await verifier.verify(make_instruction(), make_report())
        assert result.passed

    @pytest.mark.asyncio
    async def test_missing_file_fails(self):
        verifier = CIVerifier()
        report = make_report(deliverables=["/nonexistent/path/file.py"])
        result = await verifier.verify(make_instruction(), report)
        assert not result.passed
        assert any("missing_file" in f for f in result.failed_criteria)

    @pytest.mark.asyncio
    async def test_tests_failed_in_evidence_fails(self):
        verifier = CIVerifier()
        report = make_report(evidence={"tests_passed": 5, "tests_failed": 2})
        result = await verifier.verify(make_instruction(), report)
        assert not result.passed
        assert any("tests_failed" in f for f in result.failed_criteria)

    @pytest.mark.asyncio
    async def test_tests_passed_in_evidence_passes(self):
        verifier = CIVerifier()
        report = make_report(evidence={"tests_passed": 10, "tests_failed": 0})
        result = await verifier.verify(make_instruction(), report)
        assert result.passed
        assert any("tests_passed" in v for v in result.auto_verified)

    @pytest.mark.asyncio
    async def test_all_clear_passes(self):
        verifier = CIVerifier()
        result = await verifier.verify(make_instruction(), make_report())
        assert result.passed
        assert result.failed_criteria == []


class TestSemanticVerifier:
    @pytest.mark.asyncio
    async def test_mock_returns_accept(self):
        verifier = SemanticVerifier()
        ci = CIResult(passed=True, auto_verified=["file exists"])
        result = await verifier.verify(make_instruction(["criterion A"]), make_report(), ci)
        assert result.verdict == "ACCEPT"
        assert result.criteria_results.get("criterion A") == "PASS"

    @pytest.mark.asyncio
    async def test_all_criteria_in_result(self):
        criteria = ["A", "B", "C"]
        verifier = SemanticVerifier()
        ci = CIResult(passed=True)
        result = await verifier.verify(make_instruction(criteria), make_report(), ci)
        for c in criteria:
            assert c in result.criteria_results
