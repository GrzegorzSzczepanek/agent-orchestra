import asyncio
import json
import sqlite3
import sys
from pathlib import Path

import pytest
from acp.schema import McpServerStdio

from orchestra.acp_client import AgentSession, allow_once, reject_all
from orchestra.store import EventStore

MOCK = [sys.executable, "-m", "orchestra.testing.mock_agent"]


@pytest.fixture
def store(tmp_path: Path):
    s = EventStore(tmp_path / "events.db")
    yield s
    s.close()


def test_store_wal_and_schema(store: EventStore):
    assert store.journal_mode() == "wal"
    cols = {r[1] for r in sqlite3.connect(store.path).execute("PRAGMA table_info(events)")}
    assert cols == {"id", "ts", "agent", "session", "kind", "payload_json", "tokens_in", "tokens_out", "cost"}
    store.add_event("a", "x", {"k": 1}, tokens_in=5, tokens_out=7, cost=0.25)
    store.add_event("a", "y", tokens_in=1)
    store.add_event("b", "x")
    assert store.agents() == ["a", "b"]
    assert store.totals()["a"] == {"tokens_in": 6, "tokens_out": 7, "cost": 0.25}
    evs = store.events(agent="a")
    assert [e.kind for e in evs] == ["x", "y"] and evs[0].payload == {"k": 1}
    assert store.events(since_id=evs[1].id) == [store.events(agent="b")[0]]


def test_messages_inbox(store: EventStore):
    m1 = store.post_message("planner", "impl", "zrób X", thread="t1")
    store.post_message("planner", "other", "nie dla ciebie")
    store.post_message("impl", "planner", "gotowe", thread="t1")
    assert [m.body for m in store.inbox("impl")] == ["zrób X"]
    assert store.inbox("impl", since_id=m1) == []
    assert [m.body for m in store.inbox("planner", thread="t1")] == ["gotowe"]


async def test_acp_session_roundtrip(store: EventStore, tmp_path: Path):
    cwd = tmp_path / "wt"
    cwd.mkdir()
    mcp = McpServerStdio(name="orchestra", command="/bin/echo", args=["x"], env=[])
    async with AgentSession("mock", MOCK, str(cwd), store, mcp_servers=[mcp]) as s:
        res = await s.prompt("cześć")
    assert res.stop_reason == "end_turn"
    assert res.text == "echo: cześć"
    assert res.usage == {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30}
    # new_session dostał cwd i mcpServers
    info = json.loads((cwd / ".mock_session.json").read_text())
    assert info["cwd"] == str(cwd) and info["mcp_servers"][0]["name"] == "orchestra"
    kinds = [e.kind for e in store.events(agent="mock")]
    assert kinds == ["session_start", "prompt_start", "agent_thought", "agent_message", "prompt_end", "session_end"]
    assert store.totals()["mock"]["tokens_in"] == 20 and store.totals()["mock"]["tokens_out"] == 10


async def test_permission_allowed_and_rejected(store: EventStore, tmp_path: Path):
    cwd = tmp_path / "wt"
    cwd.mkdir()
    async with AgentSession("mock", MOCK, str(cwd), store, permission_handler=allow_once) as s:
        r = await s.prompt("write:a.txt:hej")
    assert (cwd / "a.txt").read_text() == "hej" and "zapisano" in r.text
    kinds = [e.kind for e in store.events(agent="mock")]
    assert {"permission_request", "permission_response", "tool_call", "tool_update"} <= set(kinds)

    async with AgentSession("mock2", MOCK, str(cwd), store, permission_handler=reject_all) as s:
        r = await s.prompt("write:b.txt:nie")
    assert not (cwd / "b.txt").exists() and "odmowa" in r.text
    resp = [e for e in store.events(agent="mock2") if e.kind == "permission_response"][0]
    assert resp.payload["option_id"] == "reject"


async def test_cancel(store: EventStore, tmp_path: Path):
    cwd = tmp_path / "wt"
    cwd.mkdir()
    async with AgentSession("mock", MOCK, str(cwd), store) as s:
        task = asyncio.create_task(s.prompt("slow"))
        await asyncio.sleep(0.5)
        await s.cancel()
        res = await asyncio.wait_for(task, 5)
    assert res.stop_reason == "cancelled"
    assert store.events(agent="mock")[-1].kind == "session_end"


async def test_missing_agent_binary_logs_error(store: EventStore, tmp_path: Path):
    with pytest.raises(Exception):
        async with AgentSession("ghost", ["definitely-not-installed-xyz"], str(tmp_path), store):
            pass
    assert store.events(agent="ghost")[-1].kind == "error"
