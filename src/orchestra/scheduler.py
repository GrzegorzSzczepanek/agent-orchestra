"""Scheduler planu: równoległe wykonanie DAG-a zadań przez workera z bramką testów i eskalacją.

Przepływ zadania:
1. worker (rola implementer, tani model) działa w osobnym worktree odgałęzionym od gałęzi integracyjnej,
2. bramka testów (test_cmd zadania, uruchamiany przez delegate),
3. opcjonalnie do `discussion_rounds` (0 do 2) rund recenzji diffu przez rolę reviewer,
4. scalenie gałęzi zadania do gałęzi integracyjnej `orchestra/run-...` (zależne zadania startują z jej czubka),
5. po `max_failures` porażkach z rzędu jedna próba eskalowana do roli planner (mocny model).
Budżet tokenów i kosztu na zadanie liczy się z logu SQLite (zdarzenia task_attempt).
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from graphlib import TopologicalSorter
from pathlib import Path
from typing import Any, Awaitable, Callable, Protocol

from .config import Config
from .delegate import DelegateResult, delegate
from .plan import Plan, PlanTask
from .store import EventStore
from .worktree import GitError, Worktree, create_worktree, git, remove_worktree

# Statusy workera, przy których ponawianie i eskalacja nie mają sensu (zła konfiguracja, brak dostępu, brak kredytów).
FATAL_STATUSES = {"config_error", "worker_not_found", "auth_failed", "insufficient_credits"}
GIT_ID = ["-c", "user.name=orchestra", "-c", "user.email=orchestra@localhost"]


@dataclass
class RunOptions:
    parallel: int = 2  # 1 do 3
    max_failures: int = 2  # kolejne porażki taniego workera przed eskalacją
    escalate: bool = True
    escalate_role: str = "planner"
    hard_to_strong: bool = True  # zadania "hard" od razu do mocnego modelu
    token_budget: int = 400_000  # tokeny (wejście + wyjście) na zadanie, 0 wyłącza limit
    cost_budget_usd: float = 0.0  # koszt na zadanie, 0 wyłącza (koszt znany tylko gdy agent go raportuje)
    discussion_rounds: int = 0  # 0 do 2 rund recenzji po zielonych testach
    worker: str = ""  # nadpisanie roli implementer
    model: str = ""
    timeout_s: float = 900

    def __post_init__(self) -> None:
        self.parallel = max(1, min(3, int(self.parallel)))
        self.discussion_rounds = max(0, min(2, int(self.discussion_rounds)))
        self.max_failures = max(1, int(self.max_failures))


@dataclass
class Verdict:
    approved: bool
    feedback: str = ""


class Reviewer(Protocol):
    async def __call__(self, task: PlanTask, result: DelegateResult) -> Verdict: ...


Executor = Callable[[PlanTask, str, str, str], Awaitable[DelegateResult]]  # task, prompt, worker, base -> wynik


@dataclass
class Attempt:
    worker: str
    status: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    note: str = ""


@dataclass
class TaskOutcome:
    id: str
    status: str  # done | failed | blocked | skipped | over_budget
    attempts: list[Attempt] = field(default_factory=list)
    escalated: bool = False
    review_rounds: int = 0
    review_unresolved: bool = False
    branch: str = ""
    error: str = ""

    @property
    def tokens(self) -> int:
        return sum(a.tokens_in + a.tokens_out for a in self.attempts)


@dataclass
class RunReport:
    run_id: str
    integration_branch: str
    outcomes: dict[str, TaskOutcome]

    @property
    def ok(self) -> bool:
        return all(o.status == "done" for o in self.outcomes.values())

    def to_dict(self) -> dict[str, Any]:
        return {"run_id": self.run_id, "integration_branch": self.integration_branch, "ok": self.ok,
                "outcomes": {k: asdict(v) for k, v in self.outcomes.items()}}

    def render(self) -> str:
        lines = [f"Przebieg {self.run_id}, gałąź integracyjna: {self.integration_branch}"]
        for o in self.outcomes.values():
            extra = " (eskalowane)" if o.escalated else ""
            lines.append(f"  [{o.status}] {o.id}: {len(o.attempts)} prób, {o.tokens} tokenów{extra}"
                         + (f", {o.error}" if o.error else ""))
        return "\n".join(lines)


def build_prompt(task: PlanTask, feedback: str = "", previous_diff: str = "", escalated: bool = False) -> str:
    parts = [f"Task `{task.id}`:\n{task.description}"]
    if task.files:
        parts.append("Files to touch: " + ", ".join(task.files))
    parts.append("Acceptance criteria:\n" + "\n".join(f"- {a}" for a in task.acceptance))
    parts.append(f"The change is accepted only when this command passes: {task.test_cmd}")
    parts.append("Work only inside the current directory. Keep the change minimal. Do not commit.")
    if escalated:
        parts.append("Previous cheaper attempts failed, you are the escalation. Fix the root cause.")
    if feedback:
        parts.append("Feedback from the previous attempt:\n" + feedback)
    if previous_diff:
        parts.append("Previous patch (start fresh, reuse what was right):\n" + previous_diff[-3000:])
    return "\n\n".join(parts)


def _feedback(res: DelegateResult) -> str:
    bits = [f"status: {res.status}"]
    if res.error:
        bits.append(f"error: {res.error[-800:]}")
    if res.test_output:
        bits.append(f"test output (tail):\n{res.test_output[-1500:]}")
    return "\n".join(bits)


class Scheduler:
    def __init__(
        self,
        config: Config,
        repo: str | Path,
        store: EventStore,
        options: RunOptions | None = None,
        *,
        executor: Executor | None = None,
        reviewer: Reviewer | None = None,
    ):
        self.config, self.repo = config, Path(repo).resolve()
        self.store = store
        self.opt = options or RunOptions()
        self.run_id = uuid.uuid4().hex[:8]
        self._executor = executor
        self.reviewer = reviewer
        self._merge_lock = asyncio.Lock()
        self._integ: Worktree | None = None
        self._abort = asyncio.Event()
        self._plan: Plan | None = None

    # --- konfiguracja pomocnicza ---
    def _worker(self) -> str:
        return self.opt.worker or self.config.roles.get("implementer", "")

    def _strong(self) -> str:
        return self.config.roles.get(self.opt.escalate_role, "")

    async def _default_executor(self, task: PlanTask, prompt: str, worker: str, base: str) -> DelegateResult:
        return await delegate(self.config, self.repo, prompt, test_cmd=task.test_cmd, worker=worker,
                              model=self.opt.model if worker == self._worker() else "", base=base,
                              timeout_s=self.opt.timeout_s, keep_worktree=True)

    def _log(self, kind: str, payload: dict[str, Any], **kw: Any) -> None:
        self.store.add_event("scheduler", kind, {"run_id": self.run_id, **payload}, **kw)

    # --- scalanie ---
    async def _integrate(self, task: PlanTask, res: DelegateResult) -> str:
        """Commit w worktree zadania i merge do gałęzi integracyjnej. Zwraca "" albo opis błędu."""
        assert self._integ is not None
        wt_path = Path(res.worktree)
        try:
            await git(["add", "-A"], wt_path)
            await git([*GIT_ID, "commit", "-q", "--allow-empty", "-m", f"orchestra: {task.id}"], wt_path)
        except GitError as exc:
            return f"commit nie powiódł się: {exc}"
        async with self._merge_lock:
            try:
                await git([*GIT_ID, "merge", "--no-ff", "-q", "-m", f"orchestra: scalono {task.id}", res.branch],
                          self._integ.path)
            except GitError as exc:
                try:
                    await git(["merge", "--abort"], self._integ.path)
                except GitError:
                    pass
                return f"konflikt scalania: {exc}"
        return ""

    async def _cleanup(self, res: DelegateResult | None) -> None:
        if res and res.worktree and Path(res.worktree).exists():
            wt = Worktree(Path(res.worktree), res.branch, "")
            try:
                await remove_worktree(self.repo, wt)
            except GitError:
                pass

    # --- jedno zadanie ---
    async def _run_task(self, task: PlanTask) -> TaskOutcome:
        assert self._integ is not None
        out = TaskOutcome(id=task.id, status="failed")
        executor = self._executor or self._default_executor
        cheap, strong = self._worker(), self._strong()
        escalated = False
        tier_strong = task.difficulty == "hard" and self.opt.hard_to_strong and bool(strong)
        tier_failures = 0
        feedback, prev_diff = "", ""
        rounds_left = self.opt.discussion_rounds if self.reviewer else 0
        last: DelegateResult | None = None

        while True:
            if self._abort.is_set():
                out.status, out.error = "skipped", "przerwano po błędzie krytycznym workera"
                break
            used = self.store.usage_for_task(self.run_id, task.id)
            if self.opt.token_budget and used["tokens_in"] + used["tokens_out"] >= self.opt.token_budget:
                out.status, out.error = "over_budget", f"limit {self.opt.token_budget} tokenów na zadanie"
                break
            if self.opt.cost_budget_usd and used["cost"] >= self.opt.cost_budget_usd:
                out.status, out.error = "over_budget", f"limit kosztu {self.opt.cost_budget_usd} USD na zadanie"
                break

            worker = strong if tier_strong else cheap
            await self._cleanup(last)
            res = await executor(task, build_prompt(task, feedback, prev_diff, escalated), worker, self._integ.branch)
            last = res
            tok_in, tok_out = int(res.usage.get("tokens_in", 0)), int(res.usage.get("tokens_out", 0))
            attempt = Attempt(worker, res.status, tok_in, tok_out, float(res.cost_usd))
            out.attempts.append(attempt)
            self.store.add_event(f"worker:{worker}", "task_attempt",
                                 {"run_id": self.run_id, "task_id": task.id, "attempt": len(out.attempts),
                                  "status": res.status, "escalated": escalated},
                                 tokens_in=tok_in, tokens_out=tok_out, cost=float(res.cost_usd))

            status, feedback, prev_diff = res.status, "", ""
            if status == "ok":
                if rounds_left > 0 and self.reviewer is not None:
                    verdict = await self.reviewer(task, res)
                    out.review_rounds += 1
                    rounds_left -= 1
                    self.store.post_message("reviewer", f"implementer:{task.id}",
                                            "APPROVE" if verdict.approved else verdict.feedback, thread=task.id)
                    if not verdict.approved:
                        attempt.note = "recenzja: uwagi"
                        feedback, prev_diff = f"Reviewer: {verdict.feedback}", res.diff
                        if rounds_left == 0:
                            out.review_unresolved = True  # limit rund: akceptujemy zielone testy, uwagi zostają w raporcie
                        else:
                            continue
                merge_err = await self._integrate(task, res)
                if not merge_err:
                    out.status, out.branch = "done", res.branch
                    break
                status, res.error = "merge_conflict", merge_err
                attempt.status = status

            if status in FATAL_STATUSES:
                out.status, out.error = "failed", f"{status}: {res.error}"
                if worker == cheap:
                    self._abort.set()  # ten sam worker zawiedzie przy następnych zadaniach, nie marnujemy czasu
                break

            feedback, prev_diff = _feedback(res), res.diff
            tier_failures += 1
            limit = 1 if escalated else self.opt.max_failures
            if tier_failures >= limit:
                if not tier_strong and self.opt.escalate and strong and strong != cheap:
                    tier_strong, escalated, tier_failures = True, True, 0
                    out.escalated = True
                    continue
                out.status, out.error = "failed", f"{status}: {res.error}"[:400]
                break

        await self._cleanup(last)  # przy sukcesie gałąź zadania jest już scalona do integracyjnej
        self._log("task_end", {"task_id": task.id, "status": out.status, "attempts": len(out.attempts),
                               "escalated": out.escalated, "tokens": out.tokens})
        return out

    # --- cały plan ---
    async def run(self, plan: Plan) -> RunReport:
        self._plan = plan
        self._integ = await create_worktree(self.repo, name=f"run-{self.run_id}", base="HEAD")
        self._log("run_start", {"tasks": len(plan.tasks), "parallel": self.opt.parallel,
                                "integration_branch": self._integ.branch})
        tasks = plan.by_id()
        ts = TopologicalSorter({t.id: set(t.depends_on) for t in plan.tasks})
        ts.prepare()
        sem = asyncio.Semaphore(self.opt.parallel)
        outcomes: dict[str, TaskOutcome] = {}
        running: dict[asyncio.Task[TaskOutcome], str] = {}

        async def guarded(t: PlanTask) -> TaskOutcome:
            async with sem:
                return await self._run_task(t)

        while ts.is_active():
            for tid in ts.get_ready():
                bad = [d for d in tasks[tid].depends_on if outcomes[d].status != "done"]
                if bad:
                    outcomes[tid] = TaskOutcome(tid, "blocked", error=f"zależność nie powiodła się: {', '.join(bad)}")
                    ts.done(tid)
                else:
                    running[asyncio.create_task(guarded(tasks[tid]))] = tid
            if not running:
                continue
            finished, _ = await asyncio.wait(running, return_when=asyncio.FIRST_COMPLETED)
            for fut in finished:
                tid = running.pop(fut)
                try:
                    outcomes[tid] = fut.result()
                except Exception as exc:  # noqa: BLE001 - błąd jednego zadania nie zatrzymuje reszty planu
                    outcomes[tid] = TaskOutcome(tid, "failed", error=f"{type(exc).__name__}: {exc}")
                ts.done(tid)

        report = RunReport(self.run_id, self._integ.branch, {t.id: outcomes[t.id] for t in plan.tasks})
        try:  # katalog worktree gałęzi integracyjnej usuwamy, sama gałąź zostaje do przeglądu i scalenia
            await git(["worktree", "remove", "--force", str(self._integ.path)], self.repo)
        except GitError:
            pass
        self._log("run_end", {"ok": report.ok, "integration_branch": report.integration_branch})
        return report


async def run_plan(plan: Plan, config: Config, repo: str | Path, store: EventStore,
                   options: RunOptions | None = None, **kw: Any) -> RunReport:
    return await Scheduler(config, repo, store, options, **kw).run(plan)
