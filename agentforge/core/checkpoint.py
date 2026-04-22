from __future__ import annotations

import os
from pathlib import Path

import aiosqlite
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver

_DB_PATH = str(Path(os.getenv("AF_DB_PATH", "agentforge.db")))
_saver: AsyncSqliteSaver | None = None


async def init_checkpointer() -> AsyncSqliteSaver:
    """Open the SQLite connection and set up the global checkpointer.
    Must be called once before get_checkpointer() is used."""
    global _saver
    if _saver is None:
        conn = await aiosqlite.connect(_DB_PATH)
        _saver = AsyncSqliteSaver(conn)
        await _saver.setup()
    return _saver


def get_checkpointer() -> AsyncSqliteSaver:
    """Return the already-initialized checkpointer. Call init_checkpointer() first."""
    if _saver is None:
        raise RuntimeError("Checkpointer not initialized. Call init_checkpointer() first.")
    return _saver
