"""
Integration test for the self-improvement loop: proposal → apply → reload guide.
All in mock mode.
"""
import os
import pytest
import tempfile
from pathlib import Path

os.environ["AF_MOCK_MODE"] = "true"

from agentforge.core.models import ImprovementProposal
from agentforge.observer.historian import Historian
from agentforge.observer.retrospective import RetrospectiveAgent
from agentforge.observer.self_improve import SelfImproveWorkflow


def make_proposal(**kwargs) -> ImprovementProposal:
    defaults = dict(
        proposal_id="int-001",
        trigger="complaint",
        problem="test problem",
        evidence=["t1: 3/5 실패"],
        root_cause="모델 티어 부족",
        change_type="config",
        target_files=["workflows/templates/feature_dev.yaml"],
        impact="L0 에스컬레이션 40% 감소",
    )
    defaults.update(kwargs)
    return ImprovementProposal(**defaults)


class TestSelfImproveIntegration:
    @pytest.mark.asyncio
    async def test_proposal_to_reload_guide(self):
        """Full flow: create proposal → apply → receive reload guide."""
        proposal = make_proposal()
        wf = SelfImproveWorkflow()
        guide = await wf.apply(proposal)

        assert guide.proposal_id == "int-001"
        assert guide.changed_files == ["workflows/templates/feature_dev.yaml"]
        assert "mock" in guide.instructions.lower()

    @pytest.mark.asyncio
    async def test_retrospective_to_self_improve_pipeline(self):
        """Retrospective produces proposal → SelfImprove applies it."""
        with tempfile.TemporaryDirectory() as tmpdir:
            retro = RetrospectiveAgent(proposal_dir=Path(tmpdir) / "proposals")
            proposal = await retro.analyze(
                Path(tmpdir) / "journal",
                trigger="complaint",
                context="왜 이렇게 실패가 많아요?",
            )
            assert proposal is not None

            wf = SelfImproveWorkflow()
            guide = await wf.apply(proposal)
            assert guide.proposal_id == proposal.proposal_id

    @pytest.mark.asyncio
    async def test_historian_and_retrospective_pipeline(self):
        """Historian records an event → Retrospective can analyze."""
        with tempfile.TemporaryDirectory() as tmpdir:
            journal_dir = Path(tmpdir) / "journal"
            journal_dir.mkdir()

            historian = Historian(journal_dir=journal_dir)
            await historian.record_complaint("U001", "버그가 너무 많아요!")

            retro = RetrospectiveAgent(proposal_dir=Path(tmpdir) / "proposals")
            # In mock mode with complaint context, should return a proposal
            proposal = await retro.analyze(
                journal_dir,
                trigger="complaint",
                context="버그가 너무 많아요!",
            )
            assert proposal is not None
            assert proposal.trigger == "complaint"
