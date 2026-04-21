from __future__ import annotations

import asyncio
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, AsyncIterator, Optional

import aiofiles

logger = logging.getLogger(__name__)

_MOCK = os.getenv("AF_MOCK_MODE", "false").lower() == "true"
_JOURNAL_DIR = Path(os.getenv("AF_JOURNAL_DIR", "memory/journal"))


class Historian:
    """
    Subscribes to LangGraph astream_events and writes structured markdown journals.
    Runs as an independent asyncio.Task alongside the main execution loop.
    """

    def __init__(self, journal_dir: Optional[Path] = None) -> None:
        self._journal_dir = journal_dir or _JOURNAL_DIR
        self._journal_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def watch(self, session_id: str, graph_events: AsyncIterator[dict]) -> None:
        """Consume graph event stream and write a journal file."""
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self._journal_dir / f"{date_str}_session_{session_id[:8]}.md"
        rows: list[dict] = []
        start_ts = datetime.now(UTC)

        async for event in graph_events:
            row = self._parse_event(event)
            if row:
                rows.append(row)

        elapsed = (datetime.now(UTC) - start_ts).total_seconds()
        await self._write_journal(path, session_id, rows, elapsed)
        logger.info("Journal written: %s (%d events)", path, len(rows))

    async def record_complaint(self, user_id: str, message: str) -> None:
        """Immediately append a complaint entry to today's journal."""
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self._journal_dir / f"{date_str}_complaints.md"
        ts = datetime.now(UTC).strftime("%H:%M:%S")
        line = f"| {ts} | {user_id} | {message[:120].replace('|', ' ')} |\n"

        if _MOCK:
            logger.info("[mock] record_complaint user=%s msg=%.60s", user_id, message)
            return

        async with aiofiles.open(path, "a", encoding="utf-8") as f:
            if not path.exists():
                await f.write("# 불만 일지\n\n| 시각 | 사용자 | 메시지 |\n|------|--------|--------|\n")
            await f.write(line)

    async def record_event(self, session_id: str, node: str, task_id: str,
                           result: str, elapsed_s: float, tokens: int) -> None:
        """Append a single event row to an in-progress journal."""
        date_str = datetime.now(UTC).strftime("%Y-%m-%d")
        path = self._journal_dir / f"{date_str}_session_{session_id[:8]}.md"
        ts = datetime.now(UTC).strftime("%H:%M")
        icon = "✅" if result == "success" else "❌" if result == "failed" else "⚙️"
        line = f"| {ts} | {node} | {task_id} | {icon} | {elapsed_s:.0f}s | {tokens:,} |\n"

        if _MOCK:
            logger.info("[mock] record_event session=%s node=%s task=%s", session_id, node, task_id)
            return

        async with aiofiles.open(path, "a", encoding="utf-8") as f:
            await f.write(line)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _parse_event(self, event: dict) -> Optional[dict]:
        event_name = event.get("event", "")
        node_name = event.get("name", "")

        if event_name not in ("on_chain_end",) or not node_name:
            return None
        if node_name in ("LangGraph", ""):
            return None

        data = event.get("data", {})
        output = data.get("output", {})
        return {
            "ts": datetime.now(UTC).strftime("%H:%M"),
            "node": node_name,
            "task_id": output.get("current_task_id", "-") if isinstance(output, dict) else "-",
            "result": "success",
            "elapsed": "-",
            "tokens": 0,
        }

    async def _write_journal(
        self, path: Path, session_id: str, rows: list[dict], elapsed: float
    ) -> None:
        if _MOCK:
            logger.info("[mock] _write_journal path=%s rows=%d", path, len(rows))
            return

        lines = [
            f"# 운영 일지 — 세션 {session_id[:8]} | {datetime.now(UTC).strftime('%Y-%m-%d %H:%M')}\n\n",
            "## 태스크 실행 기록\n",
            "| 시각 | 노드 | 태스크 | 결과 | 소요 | 토큰 |\n",
            "|------|------|--------|------|------|------|\n",
        ]
        for r in rows:
            lines.append(
                f"| {r['ts']} | {r['node']} | {r['task_id']} "
                f"| {r['result']} | {r['elapsed']} | {r['tokens']:,} |\n"
            )
        lines.append(f"\n## 완료\n총 소요: {elapsed:.1f}s\n")

        async with aiofiles.open(path, "w", encoding="utf-8") as f:
            await f.writelines(lines)
