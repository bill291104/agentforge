from __future__ import annotations

from abc import ABC, abstractmethod

from agentforge.core.models import ModelTier


class BaseAgent(ABC):
    def __init__(self, model_tier: ModelTier) -> None:
        self.model_tier = model_tier
