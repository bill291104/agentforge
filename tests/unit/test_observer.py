import os
import pytest
import tempfile
from pathlib import Path

os.environ["AF_MOCK_MODE"] = "true"

from agentforge.observer.historian import Historian
from agentforge.observer.retrospective import RetrospectiveAgent
from agentforge.observer.self_improve import SelfImproveWorkflow
from agentforge.core.models import ImprovementProposal


class TestHistorian:
    @pytest.mark.asyncio
    async def test_record_complaint_mock(self):
        """In mock mode, record_complaint should not raise."""
        h = Historian()
        await h.record_complaint("U123", "왜 이렇게 느려요?")

    @pytest.mark.asyncio
    async def test_record_event_mock(self):
        h = Historian()
        await h.record_event("sess1", "dispatch_workers", "t1", "success", 12.3, 1000)

    @pytest.mark.asyncio
    async def test_watch_empty_stream_mock(self):
        async def _empty():
            return
            yield  # make it an async generator

        h = Historian()
        # Should complete without error even with empty stream
        await h.watch("sess1", _empty())


class TestRetrospectiveAgent:
    @pytest.mark.asyncio
    async def test_no_journals_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            retro = RetrospectiveAgent(proposal_dir=Path(tmpdir) / "proposals")
            result = await retro.analyze(Path(tmpdir) / "nonexistent")
            assert result is None

    @pytest.mark.asyncio
    async def test_complaint_trigger_with_context_returns_proposal(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            retro = RetrospectiveAgent(proposal_dir=Path(tmpdir) / "proposals")
            proposal = await retro.analyze(
                Path(tmpdir) / "journal",
                trigger="complaint",
                context="시스템이 너무 느려요",
            )
            assert proposal is not None
            assert proposal.proposal_id == "mock-001"
            assert "mock" in proposal.problem

    @pytest.mark.asyncio
    async def test_no_context_complaint_returns_none(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            retro = RetrospectiveAgent(proposal_dir=Path(tmpdir) / "proposals")
            result = await retro.analyze(
                Path(tmpdir) / "journal",
                trigger="complaint",
                context="",
            )
            assert result is None

    def test_find_patterns_high_failure_rate(self):
        retro = RetrospectiveAgent()
        with tempfile.TemporaryDirectory() as tmpdir:
            journal = Path(tmpdir) / "2026-04-21_session_abc.md"
            # Write fake journal with high failure rate for task t1
            lines = ["# Test Journal\n\n"]
            lines += ["| 14:00 | dispatch | t1 | FAIL | 10s | 500 |\n"] * 4
            lines += ["| 14:00 | dispatch | t1 | OK | 10s | 500 |\n"] * 1
            journal.write_text("".join(lines), encoding="utf-8")

            patterns = retro._find_patterns([journal])
            assert len(patterns) >= 1
            pattern = next(p for p in patterns if p["task_id"] == "t1")
            assert pattern["failure_rate"] == pytest.approx(0.8)


class TestSelfImproveWorkflow:
    @pytest.mark.asyncio
    async def test_mock_returns_reload_guide(self):
        wf = SelfImproveWorkflow()
        proposal = ImprovementProposal(
            proposal_id="test-001",
            trigger="complaint",
            problem="test problem",
            evidence=[],
            root_cause="test root cause",
            change_type="config",
            target_files=["workflows/templates/feature_dev.yaml"],
            impact="test impact",
        )
        guide = await wf.apply(proposal)
        assert guide.proposal_id == "test-001"
        assert not guide.restart_required
        assert "mock" in guide.instructions.lower()

    @pytest.mark.asyncio
    async def test_mock_code_change_no_restart_override(self):
        """In mock mode, code changes also return no-restart guide."""
        wf = SelfImproveWorkflow()
        proposal = ImprovementProposal(
            proposal_id="test-002",
            trigger="auto",
            problem="code issue",
            evidence=[],
            root_cause="root",
            change_type="code",
            target_files=["agentforge/agents/worker.py"],
            impact="improvement",
        )
        guide = await wf.apply(proposal)
        assert guide.proposal_id == "test-002"
        assert "mock" in guide.instructions.lower()
