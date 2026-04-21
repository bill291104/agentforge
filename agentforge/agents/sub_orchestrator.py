from __future__ import annotations

import os

from agentforge.agents.base import BaseAgent
from agentforge.core.models import MODEL_IDS, ModelTier, TaskInstruction, TaskReport, TaskStatus


class SubOrchestrator(BaseAgent):
    """
    Sonnet-based sub-orchestrator.
    Handles a delegated sub-DAG independently, reporting back summary results.
    """

    def __init__(self) -> None:
        super().__init__(ModelTier.SONNET)
        self._model = os.getenv("AF_ORCHESTRATOR_MODEL", MODEL_IDS[ModelTier.SONNET])

    async def run_sub_dag(self, instructions: list[TaskInstruction]) -> list[TaskReport]:
        """Execute a list of instructions as an independent sub-DAG."""
        from agentforge.agents.worker import WorkerAgent
        import asyncio

        async def run_one(inst: TaskInstruction) -> TaskReport:
            worker = WorkerAgent(model_tier=inst.model_tier)
            return await worker.execute(inst)

        return await asyncio.gather(*[run_one(inst) for inst in instructions])
