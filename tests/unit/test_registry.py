import pytest

from agentforge.core.models import ModelTier
from agentforge.core.registry import AgentRegistry


@pytest.fixture
def registry():
    return AgentRegistry.with_default_pool()


class TestDefaultPool:
    def test_pool_size(self, registry):
        pool = registry.get_pool_status()
        assert len(pool) == 6  # 1 opus + 2 sonnet + 3 haiku

    def test_tiers_present(self, registry):
        tiers = [a.model_tier for a in registry.get_pool_status()]
        assert tiers.count(ModelTier.OPUS) == 1
        assert tiers.count(ModelTier.SONNET) == 2
        assert tiers.count(ModelTier.HAIKU) == 3


class TestGetBestAgent:
    def test_returns_idle_agent(self, registry):
        agent = registry.get_best_agent(ModelTier.HAIKU)
        assert agent is not None
        assert agent.status == "idle"

    def test_returns_none_when_all_busy(self, registry):
        for a in registry.get_pool_status():
            if a.model_tier == ModelTier.HAIKU:
                registry.mark_busy(a.agent_id, "t1")
        assert registry.get_best_agent(ModelTier.HAIKU) is None

    def test_prefers_higher_success_rate(self, registry):
        agents = [a for a in registry.get_pool_status() if a.model_tier == ModelTier.HAIKU]
        agents[0].success_rate_7d = 0.5
        agents[1].success_rate_7d = 0.9
        agents[2].success_rate_7d = 0.7
        best = registry.get_best_agent(ModelTier.HAIKU)
        assert best.agent_id == agents[1].agent_id


class TestMarkBusyIdle:
    def test_mark_busy(self, registry):
        agent = registry.get_best_agent(ModelTier.HAIKU)
        registry.mark_busy(agent.agent_id, "task-1")
        assert agent.status == "busy"
        assert agent.current_task_id == "task-1"

    def test_mark_idle(self, registry):
        agent = registry.get_best_agent(ModelTier.HAIKU)
        registry.mark_busy(agent.agent_id, "task-1")
        registry.mark_idle(agent.agent_id)
        assert agent.status == "idle"
        assert agent.current_task_id is None


class TestSpawnAgent:
    def test_spawn_creates_new_agent(self, registry):
        before = len(registry.get_pool_status())
        new_agent = registry.spawn_agent(ModelTier.SONNET)
        assert len(registry.get_pool_status()) == before + 1
        assert new_agent.model_tier == ModelTier.SONNET
        assert new_agent.status == "idle"


class TestUpdateStats:
    def test_success_rate_updates(self, registry):
        agent = registry.get_best_agent(ModelTier.HAIKU)
        initial_rate = agent.success_rate_7d
        registry.update_stats(agent.agent_id, success=False, duration_sec=60)
        assert agent.success_rate_7d < initial_rate
        assert agent.total_tasks_completed == 1
