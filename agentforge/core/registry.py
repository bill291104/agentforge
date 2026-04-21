from __future__ import annotations

import uuid
from typing import Optional

from agentforge.core.models import MODEL_IDS, AgentEntry, ModelTier


class AgentRegistry:
    def __init__(self) -> None:
        self._pool: dict[str, AgentEntry] = {}

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------

    def register(self, entry: AgentEntry) -> None:
        self._pool[entry.agent_id] = entry

    def get_best_agent(self, tier: ModelTier) -> Optional[AgentEntry]:
        """Return idle agent with highest 7-day success rate for the tier."""
        candidates = [
            a for a in self._pool.values()
            if a.model_tier == tier and a.status == "idle"
        ]
        if not candidates:
            return None
        return max(candidates, key=lambda a: a.success_rate_7d)

    def mark_busy(self, agent_id: str, task_id: str) -> None:
        self._pool[agent_id].status = "busy"
        self._pool[agent_id].current_task_id = task_id

    def mark_idle(self, agent_id: str) -> None:
        self._pool[agent_id].status = "idle"
        self._pool[agent_id].current_task_id = None

    def mark_failed(self, agent_id: str) -> None:
        self._pool[agent_id].status = "failed"
        self._pool[agent_id].current_task_id = None

    def update_stats(self, agent_id: str, success: bool, duration_sec: float) -> None:
        agent = self._pool[agent_id]
        total = agent.total_tasks_completed + 1
        # Exponential moving average for success rate
        alpha = 0.2
        agent.success_rate_7d = (1 - alpha) * agent.success_rate_7d + alpha * (1.0 if success else 0.0)
        agent.avg_completion_minutes = (
            (agent.avg_completion_minutes * (total - 1) + duration_sec / 60) / total
        )
        agent.total_tasks_completed = total

    def spawn_agent(self, tier: ModelTier) -> AgentEntry:
        """Create a new agent session for the given tier."""
        entry = AgentEntry(
            agent_id=f"{tier.value}-{uuid.uuid4().hex[:8]}",
            model_tier=tier,
            model_name=MODEL_IDS.get(tier, tier.value),
            status="idle",
        )
        self.register(entry)
        return entry

    def get_pool_status(self) -> list[AgentEntry]:
        return list(self._pool.values())

    # ------------------------------------------------------------------
    # Default pool initialization
    # ------------------------------------------------------------------

    @classmethod
    def with_default_pool(cls) -> "AgentRegistry":
        registry = cls()
        # Opus ×1 (리더/검증 전용)
        registry.register(AgentEntry(
            agent_id="opus-0",
            model_tier=ModelTier.OPUS,
            model_name=MODEL_IDS[ModelTier.OPUS],
            status="idle",
        ))
        # Sonnet ×2 (서브 오케스트레이터)
        for i in range(2):
            registry.register(AgentEntry(
                agent_id=f"sonnet-{i}",
                model_tier=ModelTier.SONNET,
                model_name=MODEL_IDS[ModelTier.SONNET],
                status="idle",
            ))
        # Haiku ×3 (경량 워커)
        for i in range(3):
            registry.register(AgentEntry(
                agent_id=f"haiku-{i}",
                model_tier=ModelTier.HAIKU,
                model_name=MODEL_IDS[ModelTier.HAIKU],
                status="idle",
            ))
        return registry
