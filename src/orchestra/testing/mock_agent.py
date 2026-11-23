"""Minimalny agent ACP do testów i demo TUI (bez prawdziwych modeli).

Uruchomienie: python -m orchestra.testing.mock_agent
Zachowanie zależy od treści promptu:
- "write:<plik>:<treść>" zapisuje plik w cwd sesji (po zatwierdzeniu uprawnienia),
- "slow" czeka 30 s (test anulowania),
- każdy inny tekst jest powtarzany jako odpowiedź.
Po new_session zapisuje cwd i mcp_servers do .mock_session.json w cwd.
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
            session_id=session_id, update=AgentThoughtChunk(content=TextContentBlock(text="myślę")))
        if text.startswith("write:"):
            _, name, content = text.split(":", 2)
            tc = ToolCallUpdate(tool_call_id="t1", title=f"Zapis {name}", kind="edit")
            resp = await self.conn.request_permission(
                session_id=session_id, tool_call=tc,
                options=[PermissionOption(option_id="allow", name="Zezwól", kind="allow_once"),
                         PermissionOption(option_id="reject", name="Odrzuć", kind="reject_once")])
            outcome = resp.outcome
            if getattr(outcome, "outcome", "") == "selected" and outcome.option_id == "allow":
                await self.conn.session_update(
                    session_id=session_id, update=ToolCallStart(tool_call_id="t1", title=f"Zapis {name}", kind="edit"))
                Path(self.cwd[session_id], name).write_text(content)
                await self.conn.session_update(
                    session_id=session_id, update=ToolCallProgress(tool_call_id="t1", status="completed"))
                await self._say(session_id, f"zapisano {name}")
            else:
                await self._say(session_id, f"odmowa zapisu {name}")
        else:
            await self._say(session_id, f"echo: {text}")
        return PromptResponse(stop_reason="end_turn",
                              usage=Usage(total_tokens=30, input_tokens=20, output_tokens=10))


async def main() -> None:
    await run_agent(MockAgent())


if __name__ == "__main__":
    asyncio.run(main())
