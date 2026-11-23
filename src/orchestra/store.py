"""Wspólny log zdarzeń i skrzynki wiadomości w SQLite (tryb WAL)."""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

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


class EventStore:
    """Cienka warstwa nad sqlite3. Kilka połączeń (TUI, serwer MCP, klient ACP) może pisać równolegle dzięki WAL."""

    def __init__(self, path: str | Path):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
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

    # --- zdarzenia ---
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
                (time.time(), agent, session, kind, json.dumps(payload or {}, ensure_ascii=False, default=str),
                 tokens_in, tokens_out, cost),
            )
            return int(cur.lastrowid)

    def events(self, since_id: int = 0, agent: str | None = None, limit: int = 500) -> list[Event]:
        sql, args = "SELECT * FROM events WHERE id > ?", [since_id]
        if agent:
            sql += " AND agent = ?"
            args.append(agent)
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
        """Suma tokenów i kosztu per agent."""
        with self._lock:
            rows = self._db.execute(
                "SELECT agent, SUM(tokens_in) ti, SUM(tokens_out) tout, SUM(cost) c FROM events GROUP BY agent"
            ).fetchall()
        return {r["agent"]: {"tokens_in": r["ti"] or 0, "tokens_out": r["tout"] or 0, "cost": r["c"] or 0.0}
                for r in rows}

    def usage_for_task(self, run_id: str, task_id: str) -> dict[str, float]:
        """Suma tokenów i kosztu prób zadania (zdarzenia task_attempt) w danym przebiegu."""
        with self._lock:
            row = self._db.execute(
                "SELECT SUM(tokens_in) ti, SUM(tokens_out) tout, SUM(cost) c, COUNT(*) n FROM events "
                "WHERE kind = 'task_attempt' AND json_extract(payload_json, '$.run_id') = ? "
                "AND json_extract(payload_json, '$.task_id') = ?",
                (run_id, task_id),
            ).fetchone()
        return {"tokens_in": row["ti"] or 0, "tokens_out": row["tout"] or 0, "cost": row["c"] or 0.0,
                "attempts": row["n"] or 0}

    # --- wiadomości ---
    def post_message(self, from_agent: str, to_agent: str, body: str, thread: str = "") -> int:
        with self._lock, self._db:
            cur = self._db.execute(
                "INSERT INTO messages(ts, from_agent, to_agent, thread, body) VALUES (?,?,?,?,?)",
                (time.time(), from_agent, to_agent, thread, body),
            )
            return int(cur.lastrowid)

    def inbox(self, to_agent: str, since_id: int = 0, thread: str | None = None) -> list[Message]:
        sql, args = "SELECT * FROM messages WHERE to_agent = ? AND id > ?", [to_agent, since_id]
        if thread is not None:
            sql += " AND thread = ?"
            args.append(thread)
        with self._lock:
            rows = self._db.execute(sql + " ORDER BY id", args).fetchall()
        return [Message(r["id"], r["ts"], r["from_agent"], r["to_agent"], r["thread"], r["body"]) for r in rows]
