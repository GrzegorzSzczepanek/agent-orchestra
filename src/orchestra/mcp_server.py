"""Serwer MCP (FastMCP, stdio) z narzędziem delegate, rejestrowany w Claude Code."""

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
    me = os.environ.get("ORCHESTRA_AGENT", "")  # nazwa agenta, któremu serwer jest podpięty (ustawia TUI)

    def budget_block() -> dict[str, Any] | None:
        """Gdy budżet sesji workerów jest przekroczony, narzędzia zwracają błąd zamiast uruchamiać workery."""
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
        """Deleguje proste zadanie implementacyjne tańszemu workerowi w osobnym git worktree.

        Zwraca status, diff względem bazy, wynik komendy testowej i zużycie tokenów.
        test_cmd jest uruchamiane bez powłoki (shlex), np. "pytest -q tests/test_x.py".
        worker "fake" służy do testów i zapisuje pliki z fake_files.
        red_check=true: test_cmd musi failować przed zmianami workera (inaczej status red_check_failed).
        """
        if (blocked := budget_block()) is not None:
            return blocked
        cfg.reload_overrides(default_repo)  # role i modele wybrane w TUI
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
        """Wykonuje plan zadań (DAG) tanimi workerami w tle i od razu zwraca run_id.

        Rozbij trudny cel na małe zadania: każde z `id`, `description` (samowystarczalny opis),
        `files`, `acceptance`, `test_cmd` (komenda bez powłoki, np. "pytest -q tests/test_x.py"),
        `cwd` (podkatalog repo, w którym działa worker i test_cmd, np. "code", gdy projekt nie jest w korzeniu),
        `difficulty` i `depends_on`. Zadania bez zależności idą równolegle (parallel 1 do 3).
        Każde zadanie działa w osobnym worktree odgałęzionym od HEAD (zmiany niezacommitowane
        są niewidoczne dla workerów), przechodzi bramkę test_cmd i dopiero wtedy jest scalane
        do gałęzi integracyjnej orchestra/run-<id>. Zadania, których worker nie rozwiązał, wracają
        do Ciebie (status failed) i robisz je sam.
        integration_test_cmd: pełne testy projektu (komenda bez powłoki, pełna ścieżka do binarki spoza gita), odpalane
        na gałęzi integracyjnej po każdym scaleniu. Gdy są czerwone, merge jest cofany, a zadanie wraca do workera z
        wyjściem testów. Baza musi być zielona, inaczej przebieg się nie zaczyna. Pusty łańcuch wyłącza bramkę.
        red_check (domyślnie true): test_cmd zadania musi failować przed zmianami workera, inaczej zadanie kończy się
        red_check_failed bez ponawiania (dla refaktoryzacji ustaw expect_red=false w zadaniu).
        review_rounds 1 do 2: po zielonych testach rola reviewer ocenia diff, uwagi wracają do workera (kosztuje
        zużycie roli reviewer, jej agenta zmienia się w TUI). Rozmowa recenzenta z workerem jest widoczna w wierszu inbox TUI. Postęp widać w TUI obok (orch), a stan zwraca run_status.
        """
        if (blocked := budget_block()) is not None:
            return blocked
        if integration_test_cmd.strip():
            missing = _missing_test_binary(integration_test_cmd, default_repo)
            if missing:
                return {"error": "config_error", "message": f"integration_test_cmd: nie znaleziono polecenia {missing!r}"}
        cfg.reload_overrides(default_repo)  # role i modele wybrane w TUI
        plan = Plan(goal=goal, tasks=tasks)
        opts = RunOptions(parallel=parallel, escalate=False, worker=worker, model=model,
                          token_budget=token_budget, timeout_s=timeout_s, discussion_rounds=review_rounds,
                          integration_test_cmd=integration_test_cmd, red_check=red_check)
        reviewer = (make_reviewer(headless_ask(cfg, "reviewer", default_repo, store=store))
                    if opts.discussion_rounds else None)
        sched = Scheduler(cfg, default_repo, store, opts, reviewer=reviewer)
        runs[sched.run_id] = (sched, asyncio.create_task(sched.run(plan)))
        return {"run_id": sched.run_id, "tasks": [t.id for t in plan.tasks], "order": plan.order(),
                "next": "wywołaj run_status(run_id, wait_s=60) aż finished=true"}

    @mcp.tool
    async def run_status(run_id: str, wait_s: float = 0) -> dict[str, Any]:
        """Stan przebiegu run_plan. Z wait_s > 0 czeka (do 120 s) na zakończenie, potem zwraca stan.

        Po zakończeniu zwraca report (wyniki zadań) i integration_branch do przeglądu:
        `git diff <baza>...<integration_branch>`. Scalenie do głównej gałęzi robisz Ty po recenzji diffu.
        """
        entry = runs.get(run_id)
        if entry is not None and wait_s > 0:
            await asyncio.wait([entry[1]], timeout=min(wait_s, 120))
        evs = store.events(agent="scheduler", limit=100000) + store.events(kind="task_attempt", limit=100000)
        view = build_runs(sorted(evs, key=lambda e: e.id))
        run = view.get(run_id)
        if run is None:
            return {"error": f"nieznany run_id: {run_id}", "known": sorted(runs)}
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
        """Wysyła wiadomość do innego agenta (np. do agenta z TUI). Adresat dostaje ją jako kolejny prompt.

        from_agent domyślnie jest Twoją nazwą. Zwraca id wiadomości. Nazwy agentów: list_agents.
        """
        return store.post_message(from_agent or me or "agent", to_agent, body, thread)

    @mcp.tool
    def read_inbox(agent: str = "", since_id: int = 0, thread: str | None = None) -> list[dict[str, Any]]:
        """Czyta wiadomości adresowane do agenta (domyślnie do Ciebie), nowsze niż since_id."""
        return [m.__dict__ for m in store.inbox(agent or me, since_id, thread)]

    @mcp.tool
    def list_agents() -> dict[str, Any]:
        """Lista agentów widocznych w logu (nazwy do post_message) i nazwa Twojego agenta."""
        names = [a for a in store.agents() if not a.startswith(("worker:", "role:", "task:", "scheduler", "inbox"))]
        return {"me": me, "agents": names}

    return mcp


def main() -> None:
    from .node_env import ensure_node

    ensure_node()
    build_server().run()


if __name__ == "__main__":
    main()
