"""Database access (SQLAlchemy) and operations-log helpers."""

from __future__ import annotations

import json
from contextlib import contextmanager
from functools import lru_cache
from typing import Any, Iterator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Connection, Engine

from . import config


@lru_cache(maxsize=1)
def engine() -> Engine:
    return create_engine(config.db_url(), pool_pre_ping=True, future=True)


@contextmanager
def connect() -> Iterator[Connection]:
    with engine().begin() as conn:
        yield conn


def oplog(category: str, component: str, severity: str, message: str,
          details: dict[str, Any] | None = None, run_id: int | None = None) -> int:
    """Append to ops.operations_log in its own transaction (survives a failed run)."""
    with connect() as conn:
        return conn.execute(
            text("SELECT ops.log(:c, :comp, :s, :m, CAST(:d AS jsonb), :r)"),
            {"c": category, "comp": component, "s": severity, "m": message,
             "d": json.dumps(details, default=str) if details is not None else None, "r": run_id},
        ).scalar_one()


class Run:
    """Context manager recording a pipeline run in ops.pipeline_run (+ start/finish log lines)."""

    def __init__(self, command: str, **params: Any):
        self.command, self.params = command, params
        self.run_id: int | None = None
        self.rows = 0
        self.message: str | None = None
        self.status = "success"

    def __enter__(self) -> "Run":
        with connect() as conn:
            self.run_id = conn.execute(
                text("INSERT INTO ops.pipeline_run (command, params) VALUES (:c, CAST(:p AS jsonb)) RETURNING run_id"),
                {"c": self.command, "p": json.dumps(self.params, default=str)},
            ).scalar_one()
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        if exc is not None:
            self.status, self.message = "failed", f"{exc_type.__name__}: {exc}"
            oplog("FAULT", self.command, "ERROR", f"Run {self.run_id} failed: {self.message}",
                  {"params": self.params}, self.run_id)
        with connect() as conn:
            conn.execute(
                text("UPDATE ops.pipeline_run SET status = :s, rows_written = :n, message = :m, "
                     "finished_at = now() WHERE run_id = :r"),
                {"s": self.status, "n": self.rows, "m": self.message, "r": self.run_id},
            )
        return False
