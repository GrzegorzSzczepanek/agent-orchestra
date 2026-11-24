"""Klient sterujący dla launchera `orch`: Claude Code, OpenCode, Codex albo tryb bez klienta (samo TUI).

Każdy klient dostaje ten sam serwer MCP `orchestra` (run_plan, run_status, delegate, wiadomości) i tę samą
instrukcję rozbijania zadań. Różni się tylko sposób podpięcia, bo każde narzędzie ma inną konfigurację.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

CLIENTS = ("claude", "opencode", "codex")  # kolejność autodetekcji
NONE = "none"
TOOLS = ("delegate", "run_plan", "run_status", "post_message", "read_inbox", "list_agents")
INTRO = ("Masz serwer MCP orchestra (workery tańszymi modelami). Przy trudnych zadaniach, które da się rozbić, "
         "postępuj według poniższej instrukcji.")


@dataclass
class Launch:
    argv: list[str]
    env: dict[str, str] = field(default_factory=dict)  # tylko zmienne dodawane do środowiska klienta


def detect_client(preferred: str = "") -> str:
    """Wskazany klient (jeśli zainstalowany), inaczej pierwszy dostępny, inaczej NONE (tryb bez klienta)."""
    if preferred and preferred != "auto":
        if preferred == NONE:
            return NONE
        if preferred not in CLIENTS:
            raise ValueError(f"Nieznany klient {preferred!r}, dostępne: {', '.join(CLIENTS)}, {NONE}")
        if shutil.which(preferred) is None:
            raise FileNotFoundError(f"Brak polecenia {preferred} w PATH (--client auto wybierze dostępnego)")
        return preferred
    return next((c for c in CLIENTS if shutil.which(c)), NONE)


def skill_text(skill_file: str | Path) -> str:
    """Treść skilla bez nagłówka frontmatter."""
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
        first = ("Na razie nic nie rób, potwierdź jednym zdaniem. Zapamiętaj instrukcję na kolejne zadania:\n\n" + instructions)
        return Launch(["codex", "-c", f"mcp_servers.orchestra.command={json.dumps(_server_cmd())}",
                       "-c", "mcp_servers.orchestra.args=[]", "-c", f"mcp_servers.orchestra.env={{{env}}}", *extra, first])
    raise ValueError(f"Nieznany klient: {client}")


def main(argv: list[str] | None = None) -> int:
    from .node_env import ensure_node

    ap = argparse.ArgumentParser(prog="orchestra-client", description="Uruchamia klienta (claude, opencode, codex) z serwerem orchestra")
    ap.add_argument("--client", default="auto", help=f"auto, {NONE} albo jeden z: {', '.join(CLIENTS)}")
    ap.add_argument("--repo", default=".")
    ap.add_argument("--config", required=True)
    ap.add_argument("--skill", required=True, help="plik SKILL.md z instrukcją rozbijania zadań")
    ap.add_argument("--detect", action="store_true", help="tylko wypisz wybranego klienta")
    ap.add_argument("extra", nargs="*", help="argumenty przekazane klientowi (po --)")
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
        print("Brak klienta (claude, opencode, codex) w PATH.", file=sys.stderr)
        return 3
    ensure_node()
    launch = build_launch(client, args.repo, args.config, skill_text(args.skill), args.extra)
    os.chdir(args.repo)
    os.execvpe(launch.argv[0], launch.argv, {**os.environ, **launch.env})
    return 0  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())
