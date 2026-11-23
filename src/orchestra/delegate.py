"""Delegowanie zadania workerowi w izolowanym worktree.

Worker "fake" zapisuje pliki z `fake_files` i służy do testów bez zainstalowanych agentów.
"""

from __future__ import annotations

import asyncio
import json
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .worktree import Worktree, create_worktree, diff, remove_worktree, run


@dataclass
class DelegateResult:
    status: str  # ok | tests_failed | worker_failed | worker_not_found | timeout
    worktree: str = ""
    branch: str = ""
    worker: str = ""
    worker_exit_code: int | None = None
    worker_text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)
    test_cmd: str = ""
    test_exit_code: int | None = None
    test_output: str = ""
    diff: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_command_code_output(stdout: str) -> tuple[str, dict[str, Any], str]:
    """Parsuje NDJSON z `cmd -p --output-format json`: zwraca (finalText, usage, subtype).

    Ostatnia linia ma type=result (pola: subtype, usage, finalText). Pola opcjonalne
    traktujemy ostrożnie, bo wczesne błędy je pomijają.
    """
    for line in reversed(stdout.splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") == "result":
            return obj.get("finalText", ""), obj.get("usage") or {}, obj.get("subtype", "")
    return stdout[-2000:], {}, ""


def _tail(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "...\n" + text[-limit:]


async def delegate(
    config: Config,
    repo: str | Path,
    prompt: str,
    *,
    test_cmd: str = "",
    worker: str = "",
    model: str = "",
    base: str = "HEAD",
    timeout_s: float = 900,
    keep_worktree: bool = True,
    fake_files: dict[str, str] | None = None,
) -> DelegateResult:
    """Tworzy worktree, uruchamia workera headless, odpala test_cmd i zwraca diff."""
    worker = worker or config.roles.get("implementer", "")
    wt: Worktree = await create_worktree(repo, name=prompt, base=base)
    res = DelegateResult(status="ok", worktree=str(wt.path), branch=wt.branch, worker=worker, test_cmd=test_cmd)
    try:
        if worker == "fake":
            for rel, content in (fake_files or {}).items():
                target = (wt.path / rel).resolve()
                if wt.path.resolve() not in target.parents:
                    raise ValueError(f"Ścieżka poza worktree: {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            res.worker_exit_code = 0
            res.worker_text = "fake worker"
        else:
            agent = config.agents.get(worker)
            if agent is None:
                res.status, res.error = "worker_failed", f"Nieznany worker: {worker}"
                return res
            argv = agent.headless_argv(prompt, model or None)
            try:
                code, out, err = await run(argv, wt.path, timeout=timeout_s)
            except FileNotFoundError:
                res.status, res.error = "worker_not_found", f"Brak binarki: {argv[0]}"
                return res
            except asyncio.TimeoutError:
                res.status, res.error = "timeout", f"Worker przekroczył {timeout_s}s"
                return res
            res.worker_exit_code = code
            text, usage, subtype = parse_command_code_output(out)
            res.worker_text, res.usage = _tail(text), usage
            if code != 0 or subtype in ("error", "max_turns"):
                res.status = "worker_failed"
                res.error = _tail(err or subtype)

        if test_cmd and res.status == "ok":
            tcode, tout, terr = await run(shlex.split(test_cmd), wt.path, timeout=timeout_s)
            res.test_exit_code, res.test_output = tcode, _tail(tout + terr)
            if tcode != 0:
                res.status = "tests_failed"
        res.diff = await diff(wt)
        return res
    except Exception as exc:  # noqa: BLE001 - zwracamy błąd narzędziu MCP zamiast przerywać serwer
        res.status, res.error = "worker_failed", f"{type(exc).__name__}: {exc}"
        return res
    finally:
        if not keep_worktree:
            await remove_worktree(repo, wt)
