from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH = str(Path(os.getenv("AF_DB_PATH", "agentforge.db")))
_store: "GlobalContextStore | None" = None


class GlobalContextStore:
    """
    Cross-session persistent context: user preferences, tech stack, patterns, notes.
    Stored as a single JSON document in the same SQLite DB.

    Typical keys:
      user_preferences  — {"language": "Korean", "tech_stack": ["Python", "FastAPI"]}
      project_notes     — free-text notes about ongoing project
      known_patterns    — patterns learned from past sessions
      session_stats     — {"total": N, "completed": N, "failed": N}
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS global_context (
                id         INTEGER PRIMARY KEY CHECK (id = 1),
                data       TEXT    NOT NULL DEFAULT '{}',
                updated_at TEXT    NOT NULL DEFAULT (datetime('now'))
            )
        """)
        # Ensure the single row exists
        await self._conn.execute(
            "INSERT OR IGNORE INTO global_context (id, data, updated_at) VALUES (1, '{}', ?)",
            (datetime.now(UTC).isoformat(),),
        )
        await self._conn.commit()

    async def get(self) -> dict:
        cursor = await self._conn.execute("SELECT data FROM global_context WHERE id = 1")
        row = await cursor.fetchone()
        if not row:
            return {}
        try:
            return json.loads(row[0])
        except json.JSONDecodeError:
            return {}

    async def update(self, updates: dict[str, Any]) -> None:
        data = await self.get()
        data.update(updates)
        await self._conn.execute(
            "UPDATE global_context SET data = ?, updated_at = ? WHERE id = 1",
            (json.dumps(data, ensure_ascii=False), datetime.now(UTC).isoformat()),
        )
        await self._conn.commit()
        logger.debug("Global context updated: %s", list(updates.keys()))

    async def record_session(
        self,
        session_id: str,
        success: bool,
        summary: str,
        result: str,
    ) -> None:
        """Called after each session to update stats and last-session info."""
        data = await self.get()
        stats = data.get("session_stats", {"total": 0, "completed": 0, "failed": 0})
        stats["total"] += 1
        if success:
            stats["completed"] += 1
        else:
            stats["failed"] += 1

        data["session_stats"] = stats
        data["last_session"] = {
            "session_id": session_id,
            "success": success,
            "summary": summary[:300],
            "result": result[:300],
            "timestamp": datetime.now(UTC).isoformat(),
        }
        await self._conn.execute(
            "UPDATE global_context SET data = ?, updated_at = ? WHERE id = 1",
            (json.dumps(data, ensure_ascii=False), datetime.now(UTC).isoformat()),
        )
        await self._conn.commit()

    async def get_formatted(self) -> str:
        data = await self.get()
        if not data:
            return "(글로벌 컨텍스트 없음)"

        lines: list[str] = []

        prefs = data.get("user_preferences")
        if prefs:
            lines.append("**사용자 선호도**: " + json.dumps(prefs, ensure_ascii=False))

        notes = data.get("project_notes")
        if notes:
            lines.append(f"**프로젝트 노트**: {notes}")

        patterns = data.get("known_patterns")
        if patterns:
            if isinstance(patterns, list):
                lines.append("**알려진 패턴**: " + " / ".join(patterns))
            else:
                lines.append(f"**알려진 패턴**: {patterns}")

        stats = data.get("session_stats")
        if stats:
            lines.append(
                f"**세션 통계**: 총 {stats.get('total', 0)}개 "
                f"(완료 {stats.get('completed', 0)}, 실패 {stats.get('failed', 0)})"
            )

        last = data.get("last_session")
        if last:
            status = "성공" if last.get("success") else "실패"
            lines.append(f"**마지막 세션**: {last.get('session_id', '')[:8]} — {status}")

        return "\n".join(lines) if lines else "(글로벌 컨텍스트 없음)"


async def init_global_context_store() -> GlobalContextStore:
    global _store
    if _store is None:
        conn = await aiosqlite.connect(_DB_PATH)
        _store = GlobalContextStore(conn)
        await _store.setup()
    return _store


def get_global_context_store() -> GlobalContextStore:
    if _store is None:
        raise RuntimeError("GlobalContextStore not initialized.")
    return _store
