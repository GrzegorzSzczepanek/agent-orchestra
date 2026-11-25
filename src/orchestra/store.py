"""Shared event log and message mailbox in SQLite (WAL mode)."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .commandcode import mask_secrets

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    agent TEXT NOT NULL,
    session TEXT NOT NULL DEFAULT '',
    kind TEXT NOT NULL,
    payload_json TEXT NOT NULL DEFAULT '{}',
    tokens_in INTEGER NOT NULL DEFAULT 0,
    tokens_out INTEGER NOT NULL DEFAULT 0,
    cost REAL NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_events_agent ON events(agent, id);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    from_agent TEXT NOT NULL,
    to_agent TEXT NOT NULL,
    thread TEXT NOT NULL DEFAULT '',
    body TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_to ON messages(to_agent, id);
CREATE TABLE IF NOT EXISTS delegations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    worker TEXT NOT NULL DEFAULT '',
    worker_tokens_in INTEGER NOT NULL DEFAULT 0,
    worker_tokens_out INTEGER NOT NULL DEFAULT 0,
    worker_usd REAL NOT NULL DEFAULT 0,
    spec_tokens INTEGER NOT NULL DEFAULT 0,
    review_tokens INTEGER NOT NULL DEFAULT 0,
    diff_tokens INTEGER NOT NULL DEFAULT 0,
    outcome TEXT NOT NULL,
    note TEXT NOT NULL DEFAULT ''
);
"""


@dataclass(frozen=True)
class Event:
    id: int
    ts: float
    agent: str
    session: str
    kind: str
    payload: dict[str, Any]
    tokens_in: int
    tokens_out: int
    cost: float


@dataclass(frozen=True)
class Message:
    id: int
    ts: float
    from_agent: str
    to_agent: str
    thread: str
    body: str


def _masked(value: Any) -> Any:
    """Mask secrets in every string of the payload (keys are left unchanged)."""
    if isinstance(value, str):
        return mask_secrets(value)
    if isinstance(value, dict):
        return {k: _masked(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_masked(v) for v in value]
    return value


def _make_private(path: Path) -> None:
    """The database holds agent and test output: owner only. The -wal and -shm files inherit the database permissions."""
    try:
        os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        for p in (path, Path(f"{path}-wal"), Path(f"{path}-shm")):
            if p.exists():
                p.chmod(0o600)
    except OSError:
        pass  # someone else's file or a filesystem without POSIX permissions


class EventStore:
    """Thin layer over sqlite3. Several connections (TUI, MCP server, ACP client) can write concurrently thanks to WAL."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
            _make_private(Path(self.path))
        self._lock = threading.Lock()
        self._db = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA busy_timeout=10000")
        self._db.executescript(SCHEMA)

    def close(self) -> None:
        self._db.close()

    def journal_mode(self) -> str:
        return self._db.execute("PRAGMA journal_mode").fetchone()[0]

    # --- events ---
    def add_event(
        self,
        agent: str,
        kind: str,
        payload: dict[str, Any] | None = None,
        *,
        session: str = "",
        tokens_in: int = 0,
        tokens_out: int = 0,
        cost: float = 0.0,
    ) -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO events(ts, agent, session, kind, payload_json, tokens_in, tokens_out, cost)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (time.time(), agent, session, kind, json.dumps(_masked(payload or {}), ensure_ascii=False, default=str),
                 tokens_in, tokens_out, cost),
            )
            return int(cur.lastrowid)

    def events(self, since_id: int = 0, agent: str | None = None, limit: int = 500, kind: str | None = None) -> list[Event]:
        sql, args = "SELECT * FROM events WHERE id > ?", [since_id]
        if agent:
            sql += " AND agent = ?"
            args.append(agent)
        if kind:
            sql += " AND kind = ?"
            args.append(kind)
        sql += " ORDER BY id LIMIT ?"
        args.append(limit)
        with self._lock:
            rows = self._db.execute(sql, args).fetchall()
        return [
            Event(r["id"], r["ts"], r["agent"], r["session"], r["kind"], json.loads(r["payload_json"]),
                  r["tokens_in"], r["tokens_out"], r["cost"])
            for r in rows
        ]

    def agents(self) -> list[str]:
        with self._lock:
            return [r[0] for r in self._db.execute("SELECT agent FROM events GROUP BY agent ORDER BY MIN(id)")]

    def totals(self) -> dict[str, dict[str, float]]:
        """Sum of tokens and cost per agent."""
        with self._lock:
            rows = self._db.execute(
                "SELECT agent, SUM(tokens_in) ti, SUM(tokens_out) tout, SUM(cost) c FROM events GROUP BY agent"
            ).fetchall()
        return {r["agent"]: {"tokens_in": r["ti"] or 0, "tokens_out": r["tout"] or 0, "cost": r["c"] or 0.0}
                for r in rows}

    def usage_for_task(self, run_id: str, task_id: str) -> dict[str, float]:
        """Sum of tokens and cost of task attempts (task_attempt events) in a given run."""
        with self._lock:
            row = self._db.execute(
                "SELECT SUM(tokens_in) ti, SUM(tokens_out) tout, SUM(cost) c, COUNT(*) n FROM events "
                "WHERE kind = 'task_attempt' AND json_extract(payload_json, '$.run_id') = ? "
                "AND json_extract(payload_json, '$.task_id') = ?",
                (run_id, task_id),
            ).fetchone()
        return {"tokens_in": row["ti"] or 0, "tokens_out": row["tout"] or 0, "cost": row["c"] or 0.0,
                "attempts": row["n"] or 0}

    # --- delegation ledger (cost and profitability, see ledger.py) ---
    def add_delegation(self, *, run_id: str, task_id: str, outcome: str, worker: str = "", worker_tokens_in: int = 0,
                       worker_tokens_out: int = 0, worker_usd: float = 0.0, spec_tokens: int = 0,
                       review_tokens: int = 0, diff_tokens: int = 0, note: str = "") -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO delegations(ts, run_id, task_id, worker, worker_tokens_in, worker_tokens_out, worker_usd,"
                " spec_tokens, review_tokens, diff_tokens, outcome, note) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (time.time(), run_id, task_id, worker, worker_tokens_in, worker_tokens_out, worker_usd, spec_tokens,
                 review_tokens, diff_tokens, outcome, note))
            return int(cur.lastrowid)

    def delegations(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._db.execute("SELECT * FROM delegations ORDER BY id")]

    # --- messages ---
    def post_message(self, from_agent: str, to_agent: str, body: str, thread: str = "") -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO messages(ts, from_agent, to_agent, thread, body) VALUES (?,?,?,?,?)",
                (time.time(), from_agent, to_agent, thread, body),
            )
            mid = int(cur.lastrowid)
        # TUI visibility: messages (except control run:/task:) show up in the "inbox" row
        if not to_agent.startswith(("run:", "task:")):
            self.add_event("inbox", "message", {"from": from_agent, "to": to_agent, "thread": thread, "body": body})
        return mid

    def messages(self, since_id: int = 0, limit: int = 200) -> list[Message]:
        with self._lock:
            rows = self._db.execute("SELECT * FROM messages WHERE id > ? ORDER BY id LIMIT ?", (since_id, limit)).fetchall()
        return [Message(r["id"], r["ts"], r["from_agent"], r["to_agent"], r["thread"], r["body"]) for r in rows]

    def inbox(self, to_agent: str, since_id: int = 0, thread: str | None = None) -> list[Message]:
        sql, args = "SELECT * FROM messages WHERE to_agent = ? AND id > ?", [to_agent, since_id]
        if thread is not None:
            sql += " AND thread = ?"
            args.append(thread)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id", args).fetchall()
        return [Message(r["id"], r["ts"], r["from_agent"], r["to_agent"], r["thread"], r["body"]) for r in rows]
