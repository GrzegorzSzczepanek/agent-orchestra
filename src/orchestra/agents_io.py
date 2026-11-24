"""Rozmowa z rolami (planner, reviewer) przez tryb headless lub ACP: zwraca tekst odpowiedzi."""

from __future__ import annotations

from pathlib import Path
from typing import Awaitable, Callable

from .acp_client import AgentSession, reject_all
from .commandcode import mask_secrets, parse_headless_output
from .config import Config
from .delegate import DelegateResult
from .plan import PlanTask
from .scheduler import Verdict
from .store import EventStore
from .worktree import git, run

Ask = Callable[[str], Awaitable[str]]


class AskError(RuntimeError):
    pass


def headless_ask(config: Config, role: str, repo: str | Path, *, model: str = "", timeout_s: float = 600) -> Ask:
    """Pytanie do agenta roli przez jego komendę headless (np. `claude -p ... --output-format json`)."""
    agent = config.agent_for_role(role)

    async def ask(prompt: str) -> str:
        argv = agent.headless_argv(prompt, model or None)
        try:
            code, out, err = await run(argv, repo, timeout=timeout_s)
        except FileNotFoundError as exc:
            raise AskError(f"brak binarki {argv[0]} dla roli {role}") from exc
        hr = parse_headless_output(out)
        if code != 0 or hr.subtype == "error" or hr.is_error:
            raise AskError(mask_secrets(f"rola {role} zwróciła błąd (kod {code}): {hr.error or err.strip()[-400:]}"))
        return hr.final_text if hr.found_result else out

    return ask


def acp_ask(config: Config, role: str, repo: str | Path, store: EventStore) -> Ask:
    """Pytanie do agenta roli przez ACP (nowa sesja na pytanie, uprawnienia domyślnie odrzucane)."""
    agent = config.agent_for_role(role)

    async def ask(prompt: str) -> str:
        async with AgentSession(f"{role}", agent.acp, str(repo), store, permission_handler=reject_all,
                                env=agent.acp_env() or None) as session:
            return (await session.prompt(prompt)).text

    return ask


async def repo_files(repo: str | Path, limit: int = 200) -> str:
    out = await git(["ls-files"], repo)
    lines = out.splitlines()
    more = f"\n... (+{len(lines) - limit} plików)" if len(lines) > limit else ""
    return "\n".join(lines[:limit]) + more


REVIEW_PROMPT = """Review this patch for task `{id}`.
Task: {description}
Acceptance criteria:
{acceptance}

Patch:
{diff}

Reply with exactly `APPROVE` if it is correct and minimal, otherwise `CHANGES: ` followed by concrete, short remarks."""


def make_reviewer(ask: Ask) -> Callable[[PlanTask, DelegateResult], Awaitable[Verdict]]:
    async def review(task: PlanTask, result: DelegateResult) -> Verdict:
        prompt = REVIEW_PROMPT.format(id=task.id, description=task.description,
                                      acceptance="\n".join(f"- {a}" for a in task.acceptance),
                                      diff=result.diff[-12000:])
        reply = (await ask(prompt)).strip()
        if reply.upper().startswith("APPROVE"):
            return Verdict(True)
        return Verdict(False, reply.removeprefix("CHANGES:").strip() or reply)

    return review
