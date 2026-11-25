"""Controlling client for the `orch` launcher: Claude Code, OpenCode, Codex or no client (TUI only).

Every client gets the same `orchestra` MCP server (run_plan, run_status, delegate, messages) and the same
task splitting instructions. Only the wiring differs, because each tool is configured differently.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

CLIENTS = ("claude", "opencode", "codex")  # autodetection order
NONE = "none"
TOOLS = ("delegate", "run_plan", "run_status", "post_message", "read_inbox", "list_agents")
INTRO = ("You have the orchestra MCP server (workers on cheaper models). For hard tasks that can be split, "
         "follow the instructions below.")


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)  # only variables added to the client's environment


def detect_client(preferred: str = "") -> str:
    """The requested client (if installed), otherwise the first available one, otherwise NONE (no client mode)."""
    if preferred and preferred != "auto":
        if preferred == NONE:
            return NONE
        if preferred not in CLIENTS:
            raise ValueError(f"Unknown client {preferred!r}, available: {', '.join(CLIENTS)}, {NONE}")
        if shutil.which(preferred) is None:
            raise FileNotFoundError(f"Command {preferred} not found in PATH (--client auto picks an available one)")
        return preferred
    return next((c for c in CLIENTS if shutil.which(c)), NONE)


def skill_text(skill_file: str | Path) -> str:
    """Skill body without the frontmatter header."""
    text = Path(skill_file).read_text(encoding="utf-8")
    parts = text.split("---\n", 2)
    return (parts[2] if text.startswith("---\n") and len(parts) == 3 else text).strip()


def _server_env(repo: Path, config: Path) -> dict[str, str]:
    return {"ORCHESTRA_REPO": str(repo), "ORCHESTRA_CONFIG": str(config)}


def _server_cmd() -> str:
    return str(Path(sys.executable).parent / "orchestra-mcp")


def build_launch(client: str, repo: str | Path, config: str | Path, skill: str, extra: list[str] | None = None) -> Launch:
    repo, config, extra = Path(repo).resolve(), Path(config).resolve(), list(extra or [])
    instructions = f"{INTRO}\n\n{skill}"
    if client == "claude":
        mcp = {"mcpServers": {"orchestra": {"command": _server_cmd(), "args": [], "env": _server_env(repo, config)}}}
        return Launch(["claude", "--mcp-config", json.dumps(mcp), "--allowedTools",
                       *[f"mcp__orchestra__{t}" for t in TOOLS[:3]], "--append-system-prompt", instructions, *extra])
    if client == "opencode":
        skill_path = repo / ".orchestra" / "skill.md"
        skill_path.parent.mkdir(parents=True, exist_ok=True)
        skill_path.write_text(instructions + "\n", encoding="utf-8")
        cfg = {"mcp": {"orchestra": {"type": "local", "command": [_server_cmd()], "environment": _server_env(repo, config),
                                     "enabled": True}},
               "instructions": [str(skill_path)], "permission": {"orchestra_*": "allow"}}
        return Launch(["opencode", *extra], {"OPENCODE_CONFIG_CONTENT": json.dumps(cfg)})
    if client == "codex":
        env = ", ".join(f"{k}={json.dumps(v)}" for k, v in _server_env(repo, config).items())
        first = ("Do nothing yet, confirm in one sentence. Remember these instructions for the next tasks:\n\n" + instructions)
        return Launch(["codex", "-c", f"mcp_servers.orchestra.command={json.dumps(_server_cmd())}",
                       "-c", "mcp_servers.orchestra.args=[]", "-c", f"mcp_servers.orchestra.env={{{env}}}",
                       # without this Codex asks for approval on every run_plan and run_status call
                       "-c", 'mcp_servers.orchestra.default_tools_approval_mode="approve"', *extra, first])
    raise ValueError(f"Unknown client: {client}")


def main(argv: list[str] | None = None) -> int:
    from .node_env import ensure_node

    ap = argparse.ArgumentParser(
        prog="orchestra-client", formatter_class=argparse.RawDescriptionHelpFormatter,
        description="Start a coding client (claude, opencode, codex) wired to the orchestra MCP server.\n"
                    "Usually called by the `orch` launcher.",
        epilog="Examples:\n"
               "  orchestra-client --detect --config agents.toml --skill SKILL.md\n"
               "  orchestra-client --client codex --repo . --config agents.toml --skill SKILL.md\n"
               "  orchestra-client --config agents.toml --skill SKILL.md -- --model opus")
    ap.add_argument("--client", default="auto",
                    help=f"auto (default), {NONE} or one of: {', '.join(CLIENTS)}")
    ap.add_argument("--repo", default=".", help="git repository to work in (default: current directory)")
    ap.add_argument("--config", required=True, help="path to agents.toml")
    ap.add_argument("--skill", required=True, help="SKILL.md file with the task splitting instructions")
    ap.add_argument("--detect", action="store_true", help="only print the selected client and exit")
    ap.add_argument("extra", nargs="*", help="arguments passed on to the client (after --)")
    args = ap.parse_args(argv)
    try:
        client = detect_client(args.client)
    except (ValueError, FileNotFoundError) as exc:
        print(exc, file=sys.stderr)
        return 2
    if args.detect:
        print(client)
        return 0
    if client == NONE:
        print("No client (claude, opencode, codex) found in PATH.", file=sys.stderr)
        return 3
    ensure_node()
    launch = build_launch(client, args.repo, args.config, skill_text(args.skill), args.extra)
    os.chdir(args.repo)
    os.execvpe(launch.argv[0], launch.argv, {**os.environ, **launch.env})
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
