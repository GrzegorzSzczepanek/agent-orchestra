"""External agents (and their sub-agents) announce themselves and publish status via MCP tools."""

from pathlib import Path

from fastmcp import Client

from orchestra.config import Config
from orchestra.mcp_server import build_server
from orchestra.render import render_events, status_of
from orchestra.store import EventStore


async def test_register_and_report_activity_visible(git_repo: Path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_AGENT", "cc-main")
    server = build_server(config=Config(), repo=git_repo)
    async with Client(server) as client:
        joined = (await client.call_tool("register_agent", {"role": "planner", "description": "lead"})).data
        step = (await client.call_tool("report_activity", {"text": "reading files"})).data
        done = (await client.call_tool("report_activity", {"status": "done", "text": "finished"})).data
    assert joined == {"agent": "cc-main", "role": "planner"}
    assert step == {"agent": "cc-main", "status": "working"}
    assert done == {"agent": "cc-main", "status": "done"}
    store = EventStore(git_repo / ".orchestra" / "events.db")
    assert "cc-main" in store.agents()
    evs = store.events(agent="cc-main")
    assert [e.kind for e in evs] == ["agent_joined", "agent_working", "agent_done"]
    assert status_of("agent_working", "") == "working" and status_of("agent_done", "") == "done"
    lines = [ln.plain for ln in render_events(evs)]
    assert any("agent joined (planner): lead" in ln for ln in lines)
    assert any("reading files" in ln for ln in lines)
    assert any("finished" in ln for ln in lines)


async def test_subagents_get_their_own_rows(git_repo: Path):
    server = build_server(config=Config(), repo=git_repo)
    async with Client(server) as client:
        await client.call_tool("register_agent", {"agent": "lead"})
        await client.call_tool("register_agent", {"agent": "lead/sub-1"})
        await client.call_tool("report_activity", {"agent": "lead/sub-1", "text": "editing"})
        names = (await client.call_tool("list_agents", {})).data["agents"]
    assert "lead" in names and "lead/sub-1" in names
    store = EventStore(git_repo / ".orchestra" / "events.db")
    steps = store.events(agent="lead/sub-1", kind="agent_working")
    assert [e.payload["text"] for e in steps] == ["editing"]
