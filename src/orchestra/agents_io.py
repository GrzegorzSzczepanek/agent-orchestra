"""Conversation with roles (planner, reviewer) via headless mode or ACP: returns the reply text."""

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
from .worktree import git, run, safe_env

Ask = Callable[[str], Awaitable[str]]


class AskError(RuntimeError):
    pass


def headless_ask(
    config: Config,
    role: str,
    repo: str | Path,
    *,
    model: str = "",
    timeout_s: float = 600,
    store: EventStore | None = None,
) -> Ask:
    """Question to a role's agent via its headless command (e.g. `claude -p ... --output-format json`).

    When a store is passed, each call is written to SQLite as `role_attempt` with tokens and cost,
    so the cost table also covers the planner and reviewer.
    """
    agent = config.agent_for_role(role)

    def log(status: str, hr=None) -> None:
        if store is None:
            return
        usage = (hr.usage if hr else {}) or {}
        store.add_event(f"role:{role}", "role_attempt", {"role": role, "status": status, "cache_read": int(usage.get("cache_read", 0)),
                         "cache_write": int(usage.get("cache_write", 0))},
                        tokens_in=int(usage.get("tokens_in", 0)), tokens_out=int(usage.get("tokens_out", 0)),
                        cost=float(hr.cost_usd if hr else 0.0))

    async def ask(prompt: str) -> str:
        argv = agent.headless_argv(prompt, model or None)
        try:
            code, out, err = await run(argv, repo, timeout=timeout_s, env=safe_env(agent.env_passthrough))
        except FileNotFoundError as exc:
            log("worker_not_found")
            raise AskError(f"missing binary {argv[0]} for role {role}") from exc
        hr = parse_headless_output(out)
        if code != 0 or hr.subtype == "error" or hr.is_error:
            log("error", hr)
            raise AskError(mask_secrets(f"role {role} returned an error (code {code}): {hr.error or err.strip()[-400:]}"))
        log("ok", hr)
        return hr.final_text if hr.found_result else out

    return ask


def acp_ask(config: Config, role: str, repo: str | Path, store: EventStore) -> Ask:
    """Question to a role's agent via ACP (new session per question, permissions rejected by default)."""
    agent = config.agent_for_role(role)

    async def ask(prompt: str) -> str:
        async with AgentSession(f"{role}", agent.acp, str(repo), store, permission_handler=reject_all,
                                env=agent.acp_env() or None) as session:
            return (await session.prompt(prompt)).text

    return ask


async def repo_files(repo: str | Path, limit: int = 200) -> str:
    out = await git(["ls-files"], repo)
    lines = out.splitlines()
    more = f"\n... (+{len(lines) - limit} files)" if len(lines) > limit else ""
    return "\n".join(lines[:limit]) + more


REVIEW_PROMPT = """Review this patch for task `{id}`.
Task: {description}
Acceptance criteria:
{acceptance}

Patch:
{diff}

Reply with exactly `APPROVE` if it is correct and minimal, otherwise `CHANGES: ` followed by concrete, short remarks."""


def parse_verdict(reply: str) -> Verdict:
    """APPROVE at the start or end of the reply is an approval (models like to add a preamble before the verdict).

    A `CHANGES:` occurrence wins: everything after it is remarks. A missing verdict counts as remarks.
    """
    lines = [ln.strip(" \t*`_.!") for ln in reply.splitlines() if ln.strip()]
    idx = reply.upper().find("CHANGES:")
    if idx >= 0:
        return Verdict(False, reply[idx + len("CHANGES:"):].strip() or reply)
    if lines and (lines[0].upper().startswith("APPROVE") or lines[-1].upper().endswith("APPROVE")):
        return Verdict(True)
    return Verdict(False, reply)


def make_reviewer(ask: Ask) -> Callable[[PlanTask, DelegateResult], Awaitable[Verdict]]:
    async def review(task: PlanTask, result: DelegateResult) -> Verdict:
        prompt = REVIEW_PROMPT.format(id=task.id, description=task.description,
                                      acceptance="\n".join(f"- {a}" for a in task.acceptance),
                                      diff=result.diff[-12000:])
        return parse_verdict((await ask(prompt)).strip())

    return review
