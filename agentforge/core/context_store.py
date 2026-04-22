from __future__ import annotations

import json
import logging
import os
from datetime import UTC, datetime
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

_DB_PATH = str(Path(os.getenv("AF_DB_PATH", "agentforge.db")))
_store: "ThreadContextStore | None" = None


class ThreadContextStore:
    """
    Persists Slack thread clarification state and session mappings to SQLite.
    Survives bot restarts — threads resume where they left off.

    Stages:
      clarifying  — ClarifierAgent is asking questions
      confirming  — waiting for user to click [진행/취소]
      running     — LangGraph workflow is executing
      l4_waiting  — graph is at L4 interrupt, waiting for user action
    """

    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def setup(self) -> None:
        await self._conn.execute("""
            CREATE TABLE IF NOT EXISTS thread_contexts (
                thread_ts   TEXT PRIMARY KEY,
                channel     TEXT NOT NULL,
                user_id     TEXT NOT NULL,
                stage       TEXT NOT NULL,
                request     TEXT NOT NULL,
                history     TEXT NOT NULL DEFAULT '[]',
                summary     TEXT,
                session_id  TEXT,
                task_id     TEXT,
                updated_at  TEXT NOT NULL
            )
        """)
        await self._conn.commit()

    async def save(self, thread_ts: str, state: dict) -> None:
        now = datetime.now(UTC).isoformat()
        await self._conn.execute(
            """
            INSERT INTO thread_contexts
                (thread_ts, channel, user_id, stage, request, history,
                 summary, session_id, task_id, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_ts) DO UPDATE SET
                channel    = excluded.channel,
                user_id    = excluded.user_id,
                stage      = excluded.stage,
                request    = excluded.request,
                history    = excluded.history,
                summary    = excluded.summary,
                session_id = excluded.session_id,
                task_id    = excluded.task_id,
                updated_at = excluded.updated_at
            """,
            (
                thread_ts,
                state.get("channel", ""),
                state.get("user_id", ""),
                state.get("stage", "clarifying"),
                state.get("request", ""),
                json.dumps(state.get("history", []), ensure_ascii=False),
                state.get("summary"),
                state.get("session_id"),
                state.get("task_id"),
                now,
            ),
        )
        await self._conn.commit()

    async def delete(self, thread_ts: str) -> None:
        await self._conn.execute(
            "DELETE FROM thread_contexts WHERE thread_ts = ?", (thread_ts,)
        )
        await self._conn.commit()

    async def load_all(self) -> dict[str, dict]:
        """Load all persisted thread states. Called once on startup."""
        cursor = await self._conn.execute(
            "SELECT thread_ts, channel, user_id, stage, request, "
            "history, summary, session_id, task_id FROM thread_contexts"
        )
        rows = await cursor.fetchall()
        result: dict[str, dict] = {}
        for row in rows:
            (thread_ts, channel, user_id, stage, request,
             history_json, summary, session_id, task_id) = row
            try:
                history = json.loads(history_json or "[]")
            except json.JSONDecodeError:
                history = []
            result[thread_ts] = {
                "thread_ts": thread_ts,
                "channel": channel,
                "user_id": user_id,
                "stage": stage,
                "request": request,
                "history": history,
                "summary": summary,
                "session_id": session_id,
                "task_id": task_id,
            }
        if result:
            logger.info("Loaded %d thread context(s) from DB", len(result))
        return result


async def init_context_store() -> ThreadContextStore:
    global _store
    if _store is None:
        conn = await aiosqlite.connect(_DB_PATH)
        _store = ThreadContextStore(conn)
        await _store.setup()
    return _store


def get_context_store() -> ThreadContextStore:
    if _store is None:
        raise RuntimeError("ThreadContextStore not initialized. Call init_context_store() first.")
    return _store
