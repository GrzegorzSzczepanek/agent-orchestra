"""Serwer MCP (FastMCP, stdio) z narzędziem delegate, rejestrowany w Claude Code."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .config import Config, load_config
from .delegate import delegate as run_delegate
from .store import EventStore


def build_server(config: Config | None = None, repo: str | Path | None = None) -> FastMCP:
    mcp = FastMCP(name="orchestra")
    cfg = config or load_config(os.environ.get("ORCHESTRA_CONFIG", "agents.toml"))
    default_repo = Path(repo or os.environ.get("ORCHESTRA_REPO", ".")).resolve()
    store = EventStore(os.environ.get("ORCHESTRA_DB", default_repo / ".orchestra" / "events.db"))

    @mcp.tool
    async def delegate(
        prompt: str,
        test_cmd: str = "",
        worker: str = "",
        model: str = "",
        base: str = "HEAD",
        timeout_s: float = 900,
        keep_worktree: bool = True,
        fake_files: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Deleguje proste zadanie implementacyjne tańszemu workerowi w osobnym git worktree.

        Zwraca status, diff względem bazy, wynik komendy testowej i zużycie tokenów.
        test_cmd jest uruchamiane bez powłoki (shlex), np. "pytest -q tests/test_x.py".
        worker "fake" służy do testów i zapisuje pliki z fake_files.
        """
        result = await run_delegate(
            cfg,
            default_repo,
            prompt,
            test_cmd=test_cmd,
            worker=worker,
            model=model,
            base=base,
            timeout_s=timeout_s,
            keep_worktree=keep_worktree,
            fake_files=fake_files,
        )
        return result.to_dict()

    @mcp.tool
    def post_message(from_agent: str, to_agent: str, body: str, thread: str = "") -> int:
        """Wysyła wiadomość do innego agenta (wspólna skrzynka w SQLite). Zwraca id wiadomości."""
        return store.post_message(from_agent, to_agent, body, thread)

    @mcp.tool
    def read_inbox(agent: str, since_id: int = 0, thread: str | None = None) -> list[dict[str, Any]]:
        """Czyta wiadomości adresowane do agenta, nowsze niż since_id."""
        return [m.__dict__ for m in store.inbox(agent, since_id, thread)]

    return mcp


def main() -> None:
    build_server().run()


if __name__ == "__main__":
    main()
