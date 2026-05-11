"""Checkpointer adapter."""

from __future__ import annotations

import atexit
import sqlite3
from typing import Any

_OPEN_POSTGRES_CONTEXTS: list[Any] = []


def _close_open_postgres_contexts() -> None:
    """Close any entered PostgresSaver context managers at process shutdown."""
    for cm in reversed(_OPEN_POSTGRES_CONTEXTS):
        exit_fn = getattr(cm, "__exit__", None)
        if callable(exit_fn):
            try:
                exit_fn(None, None, None)
            except Exception:
                # Best-effort cleanup only.
                pass
    _OPEN_POSTGRES_CONTEXTS.clear()


atexit.register(_close_open_postgres_contexts)


def build_checkpointer(kind: str = "memory", database_url: str | None = None) -> Any | None:
    """Return a LangGraph checkpointer.

    Supported kinds:
    - none: disable checkpoint persistence
    - memory: in-memory checkpoints (default for local dev/tests)
    - sqlite: local file-based SQL persistence
    - postgres: network SQL persistence
    """
    normalized_kind = str(kind).strip().lower()

    if normalized_kind == "none":
        return None
    if normalized_kind == "memory":
        from langgraph.checkpoint.memory import MemorySaver

        return MemorySaver()

    if normalized_kind == "sqlite":
        try:
            from langgraph.checkpoint.sqlite import SqliteSaver
        except ImportError as exc:
            raise RuntimeError("SQLite checkpointer requires: pip install langgraph-checkpoint-sqlite") from exc

        # Accept either:
        # - plain file path: "checkpoints.db"
        # - URL-like: "sqlite:///checkpoints.db"
        sqlite_target = (database_url or "checkpoints.db").strip()
        if sqlite_target.startswith("sqlite:///"):
            sqlite_target = sqlite_target.removeprefix("sqlite:///")
        elif sqlite_target.startswith("sqlite://"):
            sqlite_target = sqlite_target.removeprefix("sqlite://")

        conn = sqlite3.connect(sqlite_target, check_same_thread=False)
        # WAL improves durability/concurrency for local checkpoint writes.
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")

        # LangGraph checkpoint-sqlite 3.x expects SqliteSaver(conn=...).
        return SqliteSaver(conn=conn)

    if normalized_kind == "postgres":
        try:
            from langgraph.checkpoint.postgres import PostgresSaver
        except ImportError as exc:
            raise RuntimeError("Postgres checkpointer requires: pip install langgraph-checkpoint-postgres") from exc

        conn_string = (database_url or "").strip()
        if not conn_string:
            raise ValueError("database_url is required when checkpointer kind is 'postgres'")

        # Depending on installed version, from_conn_string may return:
        # - a BaseCheckpointSaver instance, or
        # - a context manager that yields the saver.
        maybe_saver = PostgresSaver.from_conn_string(conn_string)

        if hasattr(maybe_saver, "__enter__") and hasattr(maybe_saver, "__exit__"):
            _OPEN_POSTGRES_CONTEXTS.append(maybe_saver)
            saver = maybe_saver.__enter__()
        else:
            saver = maybe_saver

        # Some versions require explicit setup() to initialize tables.
        setup = getattr(saver, "setup", None)
        if callable(setup):
            setup()

        return saver

    raise ValueError(f"Unknown checkpointer kind: {kind}")
