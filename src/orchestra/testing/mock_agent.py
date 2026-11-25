"""Minimal ACP agent for tests and the TUI demo (without real models).

Run: python -m orchestra.testing.mock_agent
Behaviour depends on the prompt text:
- "write:<path>:<content>" writes a file in the session cwd (after the permission is approved),
- "slow" waits 30 s (cancel test),
- any other text is echoed back as the response.
After new_session it writes cwd and mcp_servers to .mock_session.json in the cwd.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from acp import PROTOCOL_VERSION, Agent, run_agent
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    InitializeResponse,
    NewSessionResponse,
    PermissionOption,
    PromptResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    Usage,
)


class MockAgent(Agent):
    def on_connect(self, conn: Any) -> None:
        self.conn = conn
        self.cwd: dict[str, str] = {}
        self.cancelled = asyncio.Event()

    async def initialize(self, protocol_version: int, client_capabilities=None, client_info=None, **kw: Any):
        return InitializeResponse(protocol_version=min(protocol_version, PROTOCOL_VERSION))

    async def new_session(self, cwd: str, additional_directories=None, mcp_servers=None, **kw: Any):
        sid = uuid4().hex[:8]
        self.cwd[sid] = cwd
        info = {"cwd": cwd, "mcp_servers": [m.model_dump(by_alias=True) for m in (mcp_servers or [])]}
        Path(cwd, ".mock_session.json").write_text(json.dumps(info))
        return NewSessionResponse(session_id=sid)

    async def cancel(self, session_id: str, **kw: Any) -> None:
        self.cancelled.set()

    async def _say(self, sid: str, text: str) -> None:
        await self.conn.session_update(
            session_id=sid, update=AgentMessageChunk(content=TextContentBlock(text=text)))

    async def prompt(self, session_id: str, prompt: list[Any], **kw: Any) -> PromptResponse:
        text = "".join(getattr(b, "text", "") for b in prompt)
        if text == "slow":
            try:
                await asyncio.wait_for(self.cancelled.wait(), 30)
            except asyncio.TimeoutError:
                pass
            return PromptResponse(stop_reason="cancelled")
        await self.conn.session_update(
            session_id=session_id, update=AgentThoughtChunk(content=TextContentBlock(text="thinking")))
        if text.startswith("write:"):
            _, name, content = text.split(":", 2)
            tc = ToolCallUpdate(tool_call_id="t1", title=f"Write {name}", kind="edit")
            resp = await self.conn.request_permission(
                session_id=session_id, tool_call=tc,
                options=[PermissionOption(option_id="allow", name="Allow", kind="allow_once"),
                         PermissionOption(option_id="reject", name="Deny", kind="reject_once")])
            outcome = resp.outcome
            if getattr(outcome, "outcome", "") == "selected" and outcome.option_id == "allow":
                await self.conn.session_update(
                    session_id=session_id, update=ToolCallStart(tool_call_id="t1", title=f"Write {name}", kind="edit"))
                Path(self.cwd[session_id], name).write_text(content)
                await self.conn.session_update(
                    session_id=session_id, update=ToolCallProgress(tool_call_id="t1", status="completed"))
                await self._say(session_id, f"saved {name}")
            else:
                await self._say(session_id, f"write refused {name}")
        else:
            await self._say(session_id, f"echo: {text}")
        return PromptResponse(stop_reason="end_turn",
                              usage=Usage(total_tokens=30, input_tokens=20, output_tokens=10))


async def main() -> None:
    await run_agent(MockAgent())


if __name__ == "__main__":
    asyncio.run(main())
