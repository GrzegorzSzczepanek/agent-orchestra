"""Klient ACP: uruchamia agenta jako podproces, normalizuje session/update do logu SQLite.

Zbudowany na oficjalnym SDK (agent-client-protocol, spawn_agent_process).
"""

from __future__ import annotations

from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Mapping, Sequence

from acp import PROTOCOL_VERSION, spawn_agent_process
from acp.interfaces import Client
from acp.schema import (
    AgentMessageChunk,
    AgentThoughtChunk,
    AllowedOutcome,
    DeniedOutcome,
    McpServerStdio,
    PermissionOption,
    RequestPermissionResponse,
    TextContentBlock,
    ToolCallProgress,
    ToolCallStart,
    ToolCallUpdate,
    UsageUpdate,
    UserMessageChunk,
)

from .store import EventStore

# (nazwa agenta, tool_call, opcje) -> option_id wybranej opcji albo None (anulowanie)
PermissionHandler = Callable[[str, ToolCallUpdate, list[PermissionOption]], Awaitable[str | None]]


async def reject_all(agent: str, tool_call: ToolCallUpdate, options: list[PermissionOption]) -> str | None:
    """Domyślna, bezpieczna polityka: wybierz pierwszą opcję reject_*, a gdy jej nie ma, anuluj."""
    for opt in options:
        if opt.kind.startswith("reject"):
            return opt.option_id
    return None


async def allow_once(agent: str, tool_call: ToolCallUpdate, options: list[PermissionOption]) -> str | None:
    for opt in options:
        if opt.kind == "allow_once":
            return opt.option_id
    return None


def _text(chunk: Any) -> str:
    content = getattr(chunk, "content", None)
    return getattr(content, "text", "") if content is not None else ""


@dataclass
class PromptResult:
    stop_reason: str
    text: str = ""
    usage: dict[str, int] = field(default_factory=dict)


class _OrchestraClient(Client):
    def __init__(self, agent: str, store: EventStore, handler: PermissionHandler):
        self.agent, self.store, self.handler = agent, store, handler
        self.text_parts: list[str] = []

    def _event(self, session_id: str, kind: str, payload: dict[str, Any], **kw: Any) -> None:
        self.store.add_event(self.agent, kind, payload, session=session_id, **kw)

    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        if isinstance(update, AgentMessageChunk):
            t = _text(update)
            self.text_parts.append(t)
            self._event(session_id, "agent_message", {"text": t})
        elif isinstance(update, AgentThoughtChunk):
            self._event(session_id, "agent_thought", {"text": _text(update)})
        elif isinstance(update, UserMessageChunk):
            self._event(session_id, "user_message", {"text": _text(update)})
        elif isinstance(update, ToolCallStart):
            self._event(session_id, "tool_call", {
                "id": update.tool_call_id, "title": update.title, "kind": update.kind,
                "status": getattr(update, "status", None)})
        elif isinstance(update, ToolCallProgress):
            self._event(session_id, "tool_update", {
                "id": update.tool_call_id, "status": update.status, "title": update.title})
        elif isinstance(update, UsageUpdate):
            cost = update.cost.amount if update.cost else 0.0
            self._event(session_id, "context", {"used": update.used, "size": update.size}, cost=cost)
        else:
            kind = getattr(update, "session_update", type(update).__name__)
            self._event(session_id, "other", {"update": str(kind)})

    async def request_permission(
        self, session_id: str, tool_call: ToolCallUpdate, options: list[PermissionOption], **kwargs: Any
    ) -> RequestPermissionResponse:
        self._event(session_id, "permission_request", {
            "tool_call_id": tool_call.tool_call_id, "title": tool_call.title,
            "options": [{"id": o.option_id, "name": o.name, "kind": o.kind} for o in options]})
        chosen = await self.handler(self.agent, tool_call, options)
        self._event(session_id, "permission_response", {"tool_call_id": tool_call.tool_call_id, "option_id": chosen})
        if chosen is None:
            return RequestPermissionResponse(outcome=DeniedOutcome())
        return RequestPermissionResponse(outcome=AllowedOutcome(option_id=chosen))


class AgentSession:
    """Jedna sesja ACP: jeden proces agenta, jeden katalog projektu (zwykle worktree)."""

    def __init__(
        self,
        name: str,
        argv: Sequence[str],
        cwd: str,
        store: EventStore,
        *,
        mcp_servers: Sequence[McpServerStdio] = (),
        permission_handler: PermissionHandler = reject_all,
        env: Mapping[str, str] | None = None,
    ):
        self.name, self.argv, self.cwd, self.store = name, list(argv), cwd, store
        self.mcp_servers, self.env = list(mcp_servers), env
        self._client = _OrchestraClient(name, store, permission_handler)
        self._stack = AsyncExitStack()
        self._conn: Any = None
        self.session_id = ""

    async def __aenter__(self) -> "AgentSession":
        try:
            self._conn, _proc = await self._stack.enter_async_context(
                spawn_agent_process(self._client, self.argv[0], *self.argv[1:], env=self.env, cwd=self.cwd)
            )
            await self._conn.initialize(protocol_version=PROTOCOL_VERSION)
            sess = await self._conn.new_session(cwd=self.cwd, mcp_servers=self.mcp_servers)
        except BaseException as exc:
            self.store.add_event(self.name, "error", {"stage": "start", "error": repr(exc)})
            await self._stack.aclose()
            raise
        self.session_id = sess.session_id
        self.store.add_event(self.name, "session_start", {"cwd": self.cwd, "argv": self.argv},
                             session=self.session_id)
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self.store.add_event(self.name, "session_end", {}, session=self.session_id)
        await self._stack.aclose()

    async def prompt(self, text: str) -> PromptResult:
        self._client.text_parts.clear()
        self.store.add_event(self.name, "prompt_start", {"text": text}, session=self.session_id)
        resp = await self._conn.prompt(session_id=self.session_id, prompt=[TextContentBlock(text=text)])
        usage: dict[str, int] = {}
        if resp.usage is not None:
            usage = {"input_tokens": resp.usage.input_tokens, "output_tokens": resp.usage.output_tokens,
                     "total_tokens": resp.usage.total_tokens}
        self.store.add_event(
            self.name, "prompt_end", {"stop_reason": resp.stop_reason, "usage": usage}, session=self.session_id,
            tokens_in=usage.get("input_tokens", 0), tokens_out=usage.get("output_tokens", 0))
        return PromptResult(resp.stop_reason, "".join(self._client.text_parts), usage)

    async def cancel(self) -> None:
        await self._conn.cancel(session_id=self.session_id)
