"""`orchestra doctor`: environment diagnostics (cmd, login, models, headless, ACP, optionally other agents).

The PASS/FAIL/SKIP/WARN report goes to the screen and to a JSON file you can paste into a chat.
No environment variable value or token ends up in the report (masked by mask_secrets).
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from acp import PROTOCOL_VERSION, spawn_agent_process
from acp.interfaces import Client

from . import __version__
from .commandcode import (
    mask_secrets,
    parse_headless_output,
    parse_models,
    resolve_model,
    status_for_exit_code,
    strip_ansi,
)
from .config import AgentConfig, Config
from .worktree import run

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"
CMD_AGENT = "commandcode"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str = "", hint: str = "") -> Check:
        c = Check(name, status, mask_secrets(detail), mask_secrets(hint))
        self.checks.append(c)
        return c

    def summary(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0, SKIP: 0, WARN: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def ok(self) -> bool:
        return self.summary()[FAIL] == 0

    def to_dict(self) -> dict[str, Any]:
        return {**self.meta, "summary": self.summary(), "checks": [asdict(c) for c in self.checks]}

    def render(self) -> str:
        lines = []
        for c in self.checks:
            lines.append(f"[{c.status}] {c.name}" + (f": {c.detail}" if c.detail else ""))
            if c.hint:
                lines.append(f"       what to do: {c.hint}")
        s = self.summary()
        lines.append("")
        lines.append(f"Summary: {s[PASS]} PASS, {s[FAIL]} FAIL, {s[WARN]} WARN, {s[SKIP]} SKIP")
        return "\n".join(lines)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class _NullClient(Client):
    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        return None

    async def request_permission(self, *a: Any, **k: Any) -> Any:  # should not happen during initialize alone
        raise RuntimeError("diagnostics do not handle permissions")


async def probe_acp(
    argv: Sequence[str], *, cwd: str, timeout: float = 30, new_session: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Starts an ACP agent, runs initialize (and optionally session/new), closes the process.

    Returns {"agent": {...}, "session": "ok" | error}. Exceptions propagate to the caller.
    """

    async def _go() -> dict[str, Any]:
        async with spawn_agent_process(_NullClient(), argv[0], *argv[1:], cwd=cwd, env=env or None) as (conn, _proc):
            init = await conn.initialize(protocol_version=PROTOCOL_VERSION)
            info = getattr(init, "agent_info", None)
            out: dict[str, Any] = {
                "agent": {"name": getattr(info, "name", ""), "version": getattr(info, "version", "")},
                "protocol_version": getattr(init, "protocol_version", None),
                "session": "skipped",
            }
            if new_session:
                try:
                    await conn.new_session(cwd=cwd, mcp_servers=[])
                    out["session"] = "ok"
                except Exception as exc:  # noqa: BLE001 - a session error (e.g. not logged in) is a diagnostic result
                    out["session"] = f"error: {exc}"
            return out

    return await asyncio.wait_for(_go(), timeout)


async def _capture(argv: Sequence[str], timeout: float, cwd: str | None = None) -> tuple[int, str, str]:
    code, out, err = await run(list(argv), cwd or tempfile.gettempdir(), timeout=timeout)
    return code, strip_ansi(out), strip_ansi(err)


def _first_line(text: str, limit: int = 200) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


_VERSION_RE = re.compile(r"^v?\d+\.\d+(\.\d+)?\S{0,30}$")


def _looks_like_version(line: str) -> bool:
    """A version is a short token (e.g. `1.93.2`), not a raw error line or a piece of minified code."""
    return bool(_VERSION_RE.match(line.split(" ")[0])) and len(line) <= 80


def _node_engines(binary: str) -> str:
    """engines.node field of the npm package that owns the binary (walk up from the realpath)."""
    exe = shutil.which(binary)
    if not exe:
        return ""
    for parent in Path(exe).resolve().parents:
        pkg = parent / "package.json"
        if pkg.is_file():
            try:
                data = json.loads(pkg.read_text())
            except (OSError, ValueError):
                continue
            if data.get("bin") or data.get("name"):
                return str(data.get("engines", {}).get("node", ""))
    return ""


def _node_satisfies(version: str, engines: str) -> bool | None:
    """Handles the `>=N[.M]` range; returns None for other notations (undecided)."""
    m = re.fullmatch(r"\s*>=\s*v?(\d+)(?:\.(\d+))?[\d.]*\s*", engines)
    v = re.match(r"v?(\d+)\.(\d+)", version)
    if not (m and v):
        return None
    return (int(v[1]), int(v[2])) >= (int(m[1]), int(m[2] or 0))


async def _diagnose_crash(binary: str, out: str, err: str, timeout: float) -> tuple[str, str]:
    """Turns a startup crash (stack trace from cli.mjs) into a readable message and hint."""
    engines = _node_engines(binary)
    node = ""
    try:
        _, nout, _ = await _capture(["node", "-v"], timeout)
        node = _first_line(nout)
    except Exception:  # noqa: BLE001
        pass
    if engines and node and _node_satisfies(node, engines) is False:
        need = engines.replace(">=", "").strip()
        return (f"{binary} does not start: Node >={need} required, you have {node}",
                f"switch Node (e.g. `nvm use {need}`) or install the package under a newer Node")
    lines = [l.strip() for l in (err or out).splitlines() if l.strip()]
    err_line = next((l for l in reversed(lines) if re.match(r"^\w*Error\b", l)), "")
    if not err_line:
        # Node truncates stderr to about 64 KB when the pipe closes, so a stack trace after a long cli.mjs line is lost
        why = "no engines field in the installed package" if not engines else f"engines {engines}"
        return (f"{binary} does not start under Node {node or '?'} ({why}, error message truncated)",
                "current command-code needs Node >=22: `nvm use 22` or `npm i -g command-code@latest` under Node 22")
    return (f"{binary} does not start ({err_line[:160]}), Node {node or '?'}",
            "run `cmd --version` by hand and check the Node version against the package engines")


async def _check_commandcode(
    rep: Report, agent: AgentConfig, *, smoke: bool, model: str, timeout: float, cwd: str
) -> None:
    binary = (agent.acp or agent.headless or ("cmd",))[0]
    if not shutil.which(binary):
        rep.add("cmd: binary", FAIL, f"`{binary}` not found in PATH",
                "install: npm i -g command-code (then restart this terminal)")
        for name in ("cmd: login", "cmd: model list", "cmd: smoke test headless", "cmd: ACP initialize",
                     "cmd: ACP session/new"):
            rep.add(name, SKIP, "skipped because the binary is missing")
        return

    try:
        code, out, err = await _capture([binary, "--version"], timeout)
        version = _first_line(out) or _first_line(err)
        crashed = code != 0 or not _looks_like_version(version)
        hint = ""
        if crashed:
            detail, hint = await _diagnose_crash(binary, out, err, timeout)
    except Exception as exc:  # noqa: BLE001
        crashed, detail, hint, version = True, f"{binary}: {type(exc).__name__}: {exc}", "", ""
    rep.meta["cmd_version"] = "" if crashed else version
    if crashed:
        rep.add("cmd: binary", FAIL, detail, hint)
        for name in ("cmd: login", "cmd: model list", "cmd: smoke test headless", "cmd: ACP initialize",
                     "cmd: ACP session/new"):
            rep.add(name, SKIP, "skipped because cmd does not start")
        return
    rep.add("cmd: binary", PASS, f"{binary} {version}")

    # login (the COMMAND_CODE_API_KEY variable takes precedence over `cmd login`)
    try:
        code, out, err = await _capture([binary, "status"], timeout)
        text = (out + err)
        if code == 0:
            rep.add("cmd: login", PASS, _first_line(text.replace("- Checking authentication status...", "")) or "logged in")
        else:
            rep.add("cmd: login", FAIL, "not logged in",
                    "run `cmd login` yourself (or set COMMAND_CODE_API_KEY, e.g. on a server without a browser)")
    except Exception as exc:  # noqa: BLE001
        rep.add("cmd: login", FAIL, f"{type(exc).__name__}: {exc}")

    # model list and the selected model
    models = []
    try:
        code, out, err = await _capture([binary, "--list-models"], timeout)
        models = parse_models(out)
        if code == 0 and models:
            rep.add("cmd: model list", PASS, f"{len(models)} models")
        else:
            rep.add("cmd: model list", FAIL, _first_line(err) or "empty list", "update: cmd update")
    except Exception as exc:  # noqa: BLE001
        rep.add("cmd: model list", FAIL, f"{type(exc).__name__}: {exc}")
    chosen = agent.resolve_model(model or None)
    if not chosen:
        rep.add("cmd: selected model", WARN, "no model set for the worker",
                f"set {agent.model_env or 'default_model'} to an ID from `cmd --list-models`, e.g. deepseek/deepseek-v4.1-flash")
    elif models:
        resolved = resolve_model(chosen, models)
        if resolved:
            rep.add("cmd: selected model", PASS, f"{chosen} -> {resolved}")
        else:
            rep.add("cmd: selected model", FAIL, f"`{chosen}` is not in the model list",
                    "pick an ID from `cmd --list-models` (full, or the part after the last '/')")
    else:
        rep.add("cmd: selected model", SKIP, f"{chosen} (cannot be verified without the model list)")

    # headless smoke test (uses credits, so only on request)
    if not smoke:
        rep.add("cmd: smoke test headless", SKIP, "skipped (uses a small amount of credits)",
                "run `orchestra doctor --smoke`; the docs do not say whether the Go plan covers headless mode")
    else:
        argv = [binary, "-p", "reply: ok", "--max-turns", "1", "--output-format", "json", "--skip-onboarding"]
        if chosen:
            argv += ["--model", chosen]
        try:
            code, out, err = await _capture(argv, max(timeout, 120), cwd=cwd)
            hr = parse_headless_output(out)
            if code == 0 and hr.subtype == "success":
                rep.add("cmd: smoke test headless", PASS,
                        f"reply: {hr.final_text[:60]!r}, tokens in/out: {hr.usage.get('tokens_in', 0)}/{hr.usage.get('tokens_out', 0)}")
            else:
                status = status_for_exit_code(code)
                hint = {
                    "auth_failed": "log in: cmd login",
                    "insufficient_credits": "out of credits or plan limit (Go: 3 USD/5 h, 6 USD/7 days, 10 USD/month)",
                    "rate_limited": "rate limited, try again in a moment",
                    "max_turns": "turn limit reached (this also means headless works)",
                }.get(status, "paste this report so the parser can be adjusted")
                st = WARN if status == "max_turns" else FAIL
                rep.add("cmd: smoke test headless", st,
                        f"{status} (code {code}, subtype={hr.subtype or '-'}): {hr.error or _first_line(err)}", hint)
        except Exception as exc:  # noqa: BLE001
            rep.add("cmd: smoke test headless", FAIL, f"{type(exc).__name__}: {exc}")

    # ACP
    argv = list(agent.acp) or [binary, "acp"]
    try:
        info = await probe_acp(argv, cwd=cwd, timeout=timeout, new_session=True, env=agent.acp_env())
        a = info["agent"]
        rep.add("cmd: ACP initialize", PASS, f"{a['name']} {a['version']}, protocol v{info['protocol_version']}")
        if info["session"] == "ok":
            rep.add("cmd: ACP session/new", PASS)
        else:
            rep.add("cmd: ACP session/new", FAIL, info["session"], "run `cmd login` yourself (auth_required)")
    except Exception as exc:  # noqa: BLE001 - asyncio.TimeoutError too
        rep.add("cmd: ACP initialize", FAIL, f"{type(exc).__name__}: {exc}",
                "check `cmd acp` by hand; the `acp` mod must not be disabled in `settings`")
        rep.add("cmd: ACP session/new", SKIP, "skipped")


async def _check_optional_agent(rep: Report, name: str, agent: AgentConfig, *, timeout: float, cwd: str) -> None:
    headless_bin = agent.headless[0] if agent.headless else ""
    acp_bin = agent.acp[0] if agent.acp else ""
    if headless_bin:
        found = shutil.which(headless_bin)
        rep.add(f"{name}: binary", PASS if found else SKIP, headless_bin if found else f"`{headless_bin}` not found in PATH")
    if not acp_bin:
        return
    if not shutil.which(acp_bin):
        rep.add(f"{name}: ACP initialize", SKIP, f"`{acp_bin}` not found in PATH")
        return
    try:
        info = await probe_acp(list(agent.acp), cwd=cwd, timeout=timeout, new_session=False, env=agent.acp_env())
        a = info["agent"]
        rep.add(f"{name}: ACP initialize", PASS, f"{a['name']} {a['version']}".strip() or "ok")
    except Exception as exc:  # noqa: BLE001
        rep.add(f"{name}: ACP initialize", FAIL, f"{type(exc).__name__}: {exc}",
                "check the `acp` command in agents.toml and the agent version")


async def run_doctor(
    config: Config,
    *,
    smoke: bool = False,
    model: str = "",
    timeout: float = 30,
    others: bool = True,
) -> Report:
    rep = Report(meta={
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "orchestra_version": __version__,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}",
    })
    with tempfile.TemporaryDirectory(prefix="orchestra-doctor-") as cwd:
        agent = config.agents.get(CMD_AGENT)
        if agent is None:
            rep.add("cmd: configuration", FAIL, f"agent `{CMD_AGENT}` missing in agents.toml")
        else:
            await _check_commandcode(rep, agent, smoke=smoke, model=model, timeout=timeout, cwd=cwd)
        if others:
            for name, ag in config.agents.items():
                if name != CMD_AGENT:
                    await _check_optional_agent(rep, name, ag, timeout=timeout, cwd=cwd)
    return rep
