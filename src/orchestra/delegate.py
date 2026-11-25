"""Delegating a task to a worker in an isolated worktree.

The "fake" worker writes files from `fake_files` and is used for tests without installed agents.
"""

from __future__ import annotations

import asyncio
import json
import os
import shlex
import shutil
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable

from .commandcode import HeadlessResult, mask_secrets, parse_headless_output, status_for_exit_code
from .config import Config, ModelRequired
from .worktree import Worktree, create_worktree, diff, remove_worktree, run, safe_env


@dataclass
class DelegateResult:
    # ok | tests_failed | worker_failed | worker_not_found | timeout | config_error
    # statuses from worker exit codes: max_turns | insufficient_credits | auth_failed | rate_limited |
    # connection_error | server_error | permission_denied | no_response | interrupted
    status: str
    worktree: str = ""
    branch: str = ""
    worker: str = ""
    worker_exit_code: int | None = None
    worker_text: str = ""
    usage: dict[str, Any] = field(default_factory=dict)  # tokens_in, tokens_out, cache_read, cache_write
    cost_usd: float = 0.0
    session_id: str = ""
    test_cmd: str = ""
    test_exit_code: int | None = None
    test_output: str = ""
    diff: str = ""
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_command_code_output(stdout: str) -> tuple[str, dict[str, Any], str]:
    """Backward compatibility: (finalText, usage, subtype). Details in commandcode.parse_headless_output."""
    hr = parse_headless_output(stdout)
    if not hr.found_result:
        return stdout[-2000:], {}, ""
    return hr.final_text, hr.usage, hr.subtype


def _tail(text: str, limit: int = 4000) -> str:
    return text if len(text) <= limit else "...\n" + text[-limit:]


def _missing_test_binary(test_cmd: str, workdir: Path) -> str:
    """Name of the first command in test_cmd if it cannot be run (otherwise an empty string)."""
    try:
        parts = shlex.split(test_cmd)
    except ValueError:
        return ""  # bad quoting will show up on the actual run
    if not parts:
        return ""
    exe = parts[0]
    if "/" in exe:
        path = Path(exe) if Path(exe).is_absolute() else workdir / exe
        return "" if path.is_file() and os.access(path, os.X_OK) else exe
    return "" if shutil.which(exe) else exe


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
    cwd: str = "",
    on_line: Callable[[str], None] | None = None,
    red_check: bool = False,
) -> DelegateResult:
    """Creates a worktree, runs the worker headless, runs test_cmd and returns the diff."""
    worker = worker or config.role_name("implementer")
    wt: Worktree = await create_worktree(repo, name=prompt, base=base)
    res = DelegateResult(status="ok", worktree=str(wt.path), branch=wt.branch, worker=worker, test_cmd=test_cmd)
    workdir = (wt.path / cwd).resolve() if cwd else wt.path
    try:
        if cwd and (wt.path.resolve() not in workdir.parents or not workdir.is_dir()):
            res.status, res.error = "worker_failed", f"cwd {cwd!r} is not a directory in the repo (at HEAD)"
            return res
        missing = _missing_test_binary(test_cmd, workdir)
        if missing:  # better to stop immediately than burn worker tokens on a task that cannot be verified
            res.status = "config_error"
            res.error = (f"test_cmd: command not found {missing!r} (PATH or directory {cwd or '.'} in the worktree). "
                         "Install it, give the full path or fix test_cmd.")
            return res
        if red_check and test_cmd:
            # the test must fail before the worker's changes; otherwise the task is empty or the test verifies nothing
            try:
                rcode, rout, rerr = await run(shlex.split(test_cmd), workdir, timeout=timeout_s,
                                              env=safe_env(config.test_env_passthrough))
            except asyncio.TimeoutError:
                res.status, res.error = "timeout", f"red check: test_cmd exceeded {timeout_s}s"
                return res
            if rcode == 0:
                res.status = "red_check_failed"
                res.test_exit_code, res.test_output = rcode, _tail(mask_secrets(rout + rerr))
                res.error = ("red check: test_cmd already passes before the worker's changes, so the task is empty or "
                             "the test verifies nothing. Fix the test or set expect_red=false.")
                return res
        if worker == "fake":
            for rel, content in (fake_files or {}).items():
                target = (wt.path / rel).resolve()
                if wt.path.resolve() not in target.parents:
                    raise ValueError(f"Path outside the worktree: {rel}")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
            res.worker_exit_code = 0
            res.worker_text = "fake worker"
        else:
            agent = config.agents.get(worker)
            if agent is None:
                res.status, res.error = "worker_failed", f"Unknown worker: {worker}"
                return res
            try:
                argv = agent.headless_argv(prompt, model or None)
            except ModelRequired as exc:
                res.status, res.error = "config_error", str(exc)
                return res
            try:
                code, out, err = await run(argv, workdir, timeout=timeout_s, on_line=on_line,
                                           env=safe_env(agent.env_passthrough))
            except FileNotFoundError:
                res.status, res.error = "worker_not_found", f"Missing binary: {argv[0]}"
                return res
            except asyncio.TimeoutError:
                res.status, res.error = "timeout", f"Worker exceeded {timeout_s}s"
                return res
            res.worker_exit_code = code
            hr: HeadlessResult = parse_headless_output(out)
            if hr.found_result:
                res.worker_text = _tail(mask_secrets(hr.final_text))
            else:
                res.worker_text = _tail(mask_secrets(out[-2000:]))
            res.usage, res.cost_usd, res.session_id = hr.usage, hr.cost_usd, hr.session_id
            if code != 0:
                res.status = status_for_exit_code(code)
            elif hr.subtype == "max_turns":
                res.status = "max_turns"
            elif hr.subtype == "error" or hr.is_error:
                res.status = "worker_failed"
            if res.status != "ok":
                res.error = _tail(mask_secrets(hr.error or err.strip() or hr.subtype or f"exit code {code}"))

        if test_cmd and res.status == "ok":
            tcode, tout, terr = await run(shlex.split(test_cmd), workdir, timeout=timeout_s,
                                          env=safe_env(config.test_env_passthrough))
            res.test_exit_code, res.test_output = tcode, _tail(mask_secrets(tout + terr))
            if tcode != 0:
                res.status = "tests_failed"
        res.diff = await diff(wt)
        return res
    except Exception as exc:  # noqa: BLE001 - return the error to the MCP tool instead of aborting the server
        res.status, res.error = "worker_failed", f"{type(exc).__name__}: {exc}"
        return res
    finally:
        if not keep_worktree:
            await remove_worktree(repo, wt)
