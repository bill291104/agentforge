from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class BaseInterface(ABC):
    """Abstract base for all user-facing interfaces (Slack, Discord, Teams, etc.)."""

    @abstractmethod
    async def start(self) -> None:
        """Start the interface (connect, listen for events)."""

    @abstractmethod
    async def stop(self) -> None:
        """Gracefully shut down the interface."""

    @abstractmethod
    async def send_message(self, channel: str, text: str, **kwargs: Any) -> Any:
        """Send a plain text message."""

    @abstractmethod
    async def update_message(self, channel: str, ts: str, text: str, **kwargs: Any) -> Any:
        """Edit/update an existing message."""

    @abstractmethod
    async def send_l4_prompt(
        self, channel: str, thread_ts: str, task_id: str, summary: str
    ) -> Any:
        """Send an interactive L4 escalation prompt (buttons for continue/abort)."""
