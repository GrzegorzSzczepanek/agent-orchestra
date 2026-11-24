"""Kontroler sesji: startuje agentów ACP (każdy w swoim worktree) i wysyła im prompty."""

from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

import os
import sys

from acp.schema import EnvVariable, McpServerStdio

from .acp_client import AgentSession, PermissionHandler, reject_all
from .config import Config
from .store import EventStore
from .worktree import create_worktree


@dataclass
class AgentSpec:
    name: str
    argv: Sequence[str]
    cwd: str
    mcp_servers: Sequence[McpServerStdio] = field(default_factory=tuple)
    env: dict[str, str] = field(default_factory=dict)


def orchestra_mcp_server(agent_name: str, repo: str | Path, config_path: str | Path, db_path: str | Path) -> McpServerStdio:
    """Serwer MCP orchestra podpinany do sesji ACP: agent dostaje post_message, read_inbox, list_agents itd."""
    env = {"ORCHESTRA_REPO": str(repo), "ORCHESTRA_CONFIG": str(config_path), "ORCHESTRA_DB": str(db_path),
           "ORCHESTRA_AGENT": agent_name, "PATH": os.environ.get("PATH", ""), "HOME": os.environ.get("HOME", "")}
    return McpServerStdio(name="orchestra", command=str(Path(sys.executable).parent / "orchestra-mcp"), args=[],
                          env=[EnvVariable(name=k, value=v) for k, v in env.items()])


async def specs_from_config(config: Config, repo: str | Path, names: Sequence[str], *,
                            config_path: str | Path | None = None, db_path: str | Path | None = None) -> list[AgentSpec]:
    """Dla każdego agenta z konfiguracji tworzy worktree i zwraca specyfikację sesji ACP.

    Z config_path i db_path agent dostaje podpięty serwer MCP orchestra (komunikacja z innymi agentami).
    """
    specs = []
    for name in names:
        agent = config.agents[name]
        wt = await create_worktree(repo, name=name)
        mcp = (orchestra_mcp_server(name, repo, config_path, db_path or Path(repo) / ".orchestra" / "events.db"),) \
            if config_path else ()
        specs.append(AgentSpec(name, agent.acp, str(wt.path), mcp_servers=mcp, env=agent.acp_env()))
    return specs


class Controller:
    def __init__(self, store: EventStore, specs: Sequence[AgentSpec], permission_handler: PermissionHandler = reject_all):
        self.store = store
        self.specs = {s.name: s for s in specs}
        self.permission_handler = permission_handler
        self.sessions: dict[str, AgentSession] = {}
        self.cwds: dict[str, str] = {s.name: s.cwd for s in specs}
        self._locks: dict[str, asyncio.Lock] = {s.name: asyncio.Lock() for s in specs}
        self._stack = AsyncExitStack()

    async def _handler(self, agent, tool_call, options):
        # indirekcja: TUI może podmienić permission_handler już po utworzeniu kontrolera
        return await self.permission_handler(agent, tool_call, options)

    async def _start(self, spec: AgentSpec) -> None:
        session = AgentSession(spec.name, spec.argv, spec.cwd, self.store,
                               mcp_servers=spec.mcp_servers, permission_handler=self._handler,
                               env=spec.env or None)
        try:
            self.sessions[spec.name] = await self._stack.enter_async_context(session)
        except Exception:  # noqa: BLE001 - błąd jest już zapisany jako zdarzenie "error"
            pass

    async def start_all(self) -> None:
        for spec in list(self.specs.values()):
            await self._start(spec)

    async def add_agent(self, spec: AgentSpec) -> None:
        """Dodaje i uruchamia nowego agenta w trakcie pracy (np. z TUI)."""
        self.specs[spec.name] = spec
        self.cwds[spec.name] = spec.cwd
        self._locks[spec.name] = asyncio.Lock()
        await self._start(spec)

    async def send(self, name: str, text: str) -> None:
        session = self.sessions.get(name)
        if session is None:
            self.store.add_event(name, "error", {"error": "sesja nie jest uruchomiona"})
            return
        async with self._locks[name]:
            try:
                await session.prompt(text)
            except Exception as exc:  # noqa: BLE001
                self.store.add_event(name, "error", {"stage": "prompt", "error": repr(exc)}, session=session.session_id)

    async def aclose(self) -> None:
        await self._stack.aclose()
