"""MCP server (FastMCP, stdio) with the delegate tool, registered in Claude Code."""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

from fastmcp import FastMCP

from .config import Config, load_config
from .delegate import _missing_test_binary, delegate as run_delegate
from .ledger import budget_state
from .plan import Plan, PlanTask
from .agents_io import headless_ask, make_reviewer
from .progress import build_runs
from .scheduler import RunOptions, RunReport, Scheduler
from .store import EventStore
from .worktree import ensure_local_exclude


def build_server(config: Config | None = None, repo: str | Path | None = None) -> FastMCP:
    mcp = FastMCP(name="orchestra")
    default_repo = Path(repo or os.environ.get("ORCHESTRA_REPO", ".")).resolve()
    ensure_local_exclude(default_repo)
    cfg = config or load_config(os.environ.get("ORCHESTRA_CONFIG", "agents.toml"), repo=default_repo)
    store = EventStore(os.environ.get("ORCHESTRA_DB", default_repo / ".orchestra" / "events.db"))
    me = os.environ.get("ORCHESTRA_AGENT", "")  # name of the agent this server is attached to (set by the TUI)

    def budget_block() -> dict[str, Any] | None:
        """When the worker session budget is exceeded, tools return an error instead of starting workers."""
        bs = budget_state(default_repo, store, cfg)
        if bs is not None and bs.exceeded:
            return {"error": "session_budget_exceeded", "message": bs.message(), "spent_usd": round(bs.spent_usd, 4),
                    "limit_usd": bs.limit_usd}
        return None

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
        red_check: bool = False,
    ) -> dict[str, Any]:
        """Delegates a simple implementation task to a cheaper worker in a separate git worktree.

        Returns the status, the diff against the base, the test command result and token usage.
        test_cmd runs without a shell (shlex), e.g. "pytest -q tests/test_x.py".
        The "fake" worker is for tests and writes the files given in fake_files.
        red_check=true: test_cmd must fail before the worker's changes (otherwise status red_check_failed).
        """
        if (blocked := budget_block()) is not None:
            return blocked
        cfg.reload_overrides(default_repo)  # roles and models chosen in the TUI
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
            red_check=red_check,
        )
        usage = result.usage or {}
        store.add_event(f"worker:{result.worker or worker}", "delegate",
                        {"status": result.status, "branch": result.branch, "exit_code": result.worker_exit_code,
                         "cache_read": int(usage.get("cache_read", 0)), "cache_write": int(usage.get("cache_write", 0))},
                        tokens_in=int(usage.get("tokens_in", 0)), tokens_out=int(usage.get("tokens_out", 0)),
                        cost=float(result.cost_usd))
        return result.to_dict()

    runs: dict[str, tuple[Scheduler, asyncio.Task[RunReport]]] = {}

    @mcp.tool
    async def run_plan(
        goal: str,
        tasks: list[PlanTask],
        parallel: int = 2,
        worker: str = "",
        model: str = "",
        token_budget: int = 400_000,
        timeout_s: float = 900,
        review_rounds: int = 0,
        integration_test_cmd: str = "",
        red_check: bool = True,
    ) -> dict[str, Any]:
        """Runs a task plan (DAG) with cheap workers in the background and returns a run_id right away.

        Split a hard goal into small tasks, each with `id`, `description` (self-contained),
        `files`, `acceptance`, `test_cmd` (command without a shell, e.g. "pytest -q tests/test_x.py"),
        `cwd` (repo subdirectory where the worker and test_cmd run, e.g. "code" when the project is not at the root),
        `difficulty` and `depends_on`. Tasks without dependencies run in parallel (parallel 1 to 3).
        Each task runs in its own worktree branched from HEAD (uncommitted changes are invisible to workers),
        has to pass its test_cmd gate and only then is merged into the integration branch
        orchestra/run-<id>. Tasks the worker could not solve come back to you (status failed) and you do them.
        integration_test_cmd: full project tests (command without a shell, full path to a binary outside git), run
        on the integration branch after every merge. When red, the merge is reverted and the task goes back to the
        worker with the test output. The base must be green or the run does not start. Empty string disables the gate.
        red_check (default true): a task's test_cmd must fail before the worker's changes, otherwise the task ends
        with red_check_failed and is not retried (for refactors set expect_red=false on the task).
        review_rounds 1 to 2: after green tests the reviewer role judges the diff and its remarks go back to the worker
        (uses the reviewer role's quota, its agent is changed in the TUI). The reviewer and worker conversation is
        visible in the TUI inbox row. Progress shows in the TUI next to you (orch), run_status returns the state.
        """
        if (blocked := budget_block()) is not None:
            return blocked
        if integration_test_cmd.strip():
            missing = _missing_test_binary(integration_test_cmd, default_repo)
            if missing:
                return {"error": "config_error", "message": f"integration_test_cmd: command not found {missing!r}"}
        cfg.reload_overrides(default_repo)  # roles and models chosen in the TUI
        plan = Plan(goal=goal, tasks=tasks)
        opts = RunOptions(parallel=parallel, escalate=True, worker=worker, model=model,
                          token_budget=token_budget, timeout_s=timeout_s, discussion_rounds=review_rounds,
                          integration_test_cmd=integration_test_cmd, red_check=red_check)
        reviewer = (make_reviewer(headless_ask(cfg, "reviewer", default_repo, store=store))
                    if opts.discussion_rounds else None)
        sched = Scheduler(cfg, default_repo, store, opts, reviewer=reviewer)
        runs[sched.run_id] = (sched, asyncio.create_task(sched.run(plan)))
        return {"run_id": sched.run_id, "tasks": [t.id for t in plan.tasks], "order": plan.order(),
                "next": "call run_status(run_id, wait_s=60) until finished=true"}

    @mcp.tool
    async def run_status(run_id: str, wait_s: float = 0) -> dict[str, Any]:
        """State of a run_plan run. With wait_s > 0 waits (up to 120 s) for it to finish, then returns the state.

        Once finished returns the report (task outcomes) and the integration_branch to review:
        `git diff <base>...<integration_branch>`. Merging into the main branch is your job after reviewing the diff.
        """
        entry = runs.get(run_id)
        if entry is not None and wait_s > 0:
            await asyncio.wait([entry[1]], timeout=min(wait_s, 120))
        evs = store.events(agent="scheduler", limit=100000) + store.events(kind="task_attempt", limit=100000)
        view = build_runs(sorted(evs, key=lambda e: e.id))
        run = view.get(run_id)
        if run is None:
            return {"error": f"unknown run_id: {run_id}", "known": sorted(runs)}
        out: dict[str, Any] = run.to_dict()
        if entry is not None and entry[1].done():
            exc = entry[1].exception()
            if exc is not None:
                out["error"] = f"{type(exc).__name__}: {exc}"
            else:
                out["report"] = entry[1].result().to_dict()
        return out

    @mcp.tool
    def post_message(to_agent: str, body: str, from_agent: str = "", thread: str = "") -> int:
        """Sends a message to another agent (e.g. one in the TUI). The recipient gets it as its next prompt.

        from_agent defaults to your name. Returns the message id. Agent names: list_agents.
        """
        return store.post_message(from_agent or me or "agent", to_agent, body, thread)

    @mcp.tool
    def read_inbox(agent: str = "", since_id: int = 0, thread: str | None = None) -> list[dict[str, Any]]:
        """Reads messages addressed to an agent (you by default) newer than since_id."""
        return [m.__dict__ for m in store.inbox(agent or me, since_id, thread)]

    @mcp.tool
    def list_agents() -> dict[str, Any]:
        """Lists agents visible in the log (names for post_message) and your own agent name."""
        names = [a for a in store.agents() if not a.startswith(("worker:", "role:", "task:", "scheduler", "inbox"))]
        return {"me": me, "agents": names}

    return mcp


def main() -> None:
    from .node_env import ensure_node

    ensure_node()
    build_server().run()


if __name__ == "__main__":
    main()
