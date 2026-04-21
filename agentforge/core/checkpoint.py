from __future__ import annotations

import os
import sqlite3
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver

_DB_PATH = str(Path(os.getenv("AF_DB_PATH", "agentforge.db")))
_saver: SqliteSaver | None = None


def get_checkpointer() -> SqliteSaver:
    """Return a singleton SqliteSaver for session persistence."""
    global _saver
    if _saver is None:
        conn = sqlite3.connect(_DB_PATH, check_same_thread=False)
        _saver = SqliteSaver(conn)
    return _saver
