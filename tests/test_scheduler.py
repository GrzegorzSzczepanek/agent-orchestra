import sys
from pathlib import Path

import pytest

from orchestra.config import Config
from orchestra.delegate import delegate
from orchestra.plan import Plan, PlanError, PlanTask, extract_json, make_plan, parse_plan, plan_schema
from orchestra.scheduler import RunOptions, Verdict, run_plan
from orchestra.store import EventStore
from orchestra.worktree import git

CFG = Config(agents={}, roles={"implementer": "cheap", "planner": "strong", "reviewer": "strong"})


def task(tid, deps=(), difficulty="easy", test=None, **kw):
    return {"id": tid, "description": f"do {tid}", "files": [f"{tid}.py"], "acceptance": [f"{tid} works"],
            "test_cmd": test or f"{sys.executable} -m pytest -q test_{tid}.py", "difficulty": difficulty,
            "depends_on": list(deps), **kw}


def plan_of(*tasks):
    return Plan.model_validate({"goal": "goal", "tasks": list(tasks)})


def src_test(tid):
    return f"from {tid} import VALUE\n\ndef test_{tid}():\n    assert VALUE == '{tid}'\n"


class FakeExec:
    """Executor built on the real delegate with a fake worker: files depend on the task, attempt and worker name."""

    def __init__(self, behaviour):
        self.behaviour = behaviour  # (task_id, worker, attempt_no) -> dict of files or None (do nothing)
        self.calls: list[tuple[str, str, int, str]] = []
        self.prompts: list[str] = []
        self.counts: dict[str, int] = {}

    async def __call__(self, task: PlanTask, prompt: str, worker: str, base: str):
        n = self.counts[task.id] = self.counts.get(task.id, 0) + 1
        self.calls.append((task.id, worker, n, base))
        self.prompts.append(prompt)
        files = self.behaviour(task.id, worker, n) or {}
        res = await delegate(CFG, self.repo, prompt, test_cmd=task.test_cmd, worker="fake", fake_files=files,
                             base=base, keep_worktree=True)
        res.usage = {"tokens_in": 100, "tokens_out": 10}
        return res


def good(tid):
    return {f"{tid}.py": f"VALUE = '{tid}'\n", f"test_{tid}.py": src_test(tid)}


@pytest.fixture
def env(git_repo: Path):
    store = EventStore(":memory:")
    return git_repo, store


def make_exec(repo, behaviour):
    ex = FakeExec(behaviour)
    ex.repo = repo
    return ex


# --- plan ---
def test_plan_validation_cycle_missing_dup():
    with pytest.raises(ValueError, match="cycle"):
        plan_of(task("a", ["b"]), task("b", ["a"]))
    with pytest.raises(ValueError, match="unknown"):
        plan_of(task("a", ["zz"]))
    with pytest.raises(ValueError, match="duplicate"):
        plan_of(task("a"), task("a"))
    with pytest.raises(ValueError):
        plan_of(task("a", files=["../etc/passwd"]))
    with pytest.raises(ValueError):
        plan_of(task("A B"))
    assert plan_of(task("a"), task("b", ["a"])).order() == ["a", "b"]
    assert {"id", "description", "files", "acceptance", "test_cmd", "difficulty", "depends_on"} <= set(
        plan_schema()["$defs"]["PlanTask"]["properties"])


def test_extract_json_variants():
    assert extract_json('text {"a": 1} end') == {"a": 1}
    assert extract_json('```json\n{"a": {"b": 2}}\n```') == {"a": {"b": 2}}
    with pytest.raises(PlanError):
        extract_json("no json")


async def test_make_plan_retries_once_with_error_message():
    import json

    prompts = []
    valid = {"goal": "g", "tasks": [task("a")]}

    async def ask(p):
        prompts.append(p)
        return "not json" if len(prompts) == 1 else "```json\n" + json.dumps(valid) + "\n```"

    plan = await make_plan("goal", ask, repo_files="a.py")
    assert plan.tasks[0].id == "a" and len(prompts) == 2 and "invalid" in prompts[1]

    async def always_bad(p):
        return "{}"

    with pytest.raises(PlanError):
        await make_plan("goal", always_bad, retries=1)


# --- scheduler ---
async def test_runs_dag_and_merges_into_integration_branch(env):
    repo, store = env
    ex = make_exec(repo, lambda tid, w, n: good(tid))
    plan = plan_of(task("a"), task("b", ["a"]), task("c"))
    rep = await run_plan(plan, CFG, repo, store, RunOptions(parallel=2), executor=ex)
    assert rep.ok, rep.render()
    assert all(o.status == "done" and len(o.attempts) == 1 for o in rep.outcomes.values())
    # b starts from the integration branch, which already contains a
    tree = await git(["ls-tree", "-r", "--name-only", rep.integration_branch], repo)
    assert {"a.py", "b.py", "c.py", "test_a.py"} <= set(tree.split())
    # base repo (main) untouched, and the integration worktree directory removed
    assert "a.py" not in (await git(["ls-tree", "-r", "--name-only", "main"], repo))
    assert not list((repo / ".orchestra" / "worktrees").glob("*"))
    ends = [e for e in store.events(agent="scheduler") if e.kind == "task_end"]
    assert len(ends) == 3


async def test_parallelism_is_clamped_and_respected(env):
    repo, store = env
    assert RunOptions(parallel=99).parallel == 3 and RunOptions(parallel=0).parallel == 1
    assert RunOptions(discussion_rounds=9).discussion_rounds == 2 and RunOptions().discussion_rounds == 0
    import asyncio

    running = peak = 0
    inner = make_exec(repo, lambda tid, w, n: good(tid))

    async def ex(t, p, w, b):
        nonlocal running, peak
        running += 1
        peak = max(peak, running)
        await asyncio.sleep(0.15)
        running -= 1
        return await inner(t, p, w, b)

    plan = plan_of(*[task(f"t{i}") for i in range(5)])
    rep = await run_plan(plan, CFG, repo, store, RunOptions(parallel=2), executor=ex)
    assert rep.ok and peak == 2


async def test_gate_failure_retries_then_escalates_to_planner_role(env):
    repo, store = env
    # the cheap worker changes nothing (tests fail), the strong model fixes it
    ex = make_exec(repo, lambda tid, w, n: good(tid) if w == "strong" else {f"test_{tid}.py": src_test(tid)})
    rep = await run_plan(plan_of(task("a")), CFG, repo, store, RunOptions(max_failures=2), executor=ex)
    o = rep.outcomes["a"]
    assert rep.ok and o.escalated
    assert [a.worker for a in o.attempts] == ["cheap", "cheap", "strong"]
    assert [a.status for a in o.attempts][:2] == ["tests_failed", "tests_failed"]


async def test_failure_after_escalation_blocks_dependents_but_not_independent(env):
    repo, store = env
    ex = make_exec(repo, lambda tid, w, n: good(tid) if tid == "ok" else {f"test_{tid}.py": src_test(tid)})
    plan = plan_of(task("bad"), task("child", ["bad"]), task("ok"))
    rep = await run_plan(plan, CFG, repo, store, RunOptions(max_failures=2), executor=ex)
    assert rep.outcomes["bad"].status == "failed" and len(rep.outcomes["bad"].attempts) == 3
    assert rep.outcomes["child"].status == "blocked"
    assert rep.outcomes["ok"].status == "done"
    assert not rep.ok
    assert "child" not in {c[0] for c in ex.calls}


async def test_hard_task_goes_straight_to_strong(env):
    repo, store = env
    ex = make_exec(repo, lambda tid, w, n: good(tid))
    rep = await run_plan(plan_of(task("h", difficulty="hard")), CFG, repo, store, executor=ex)
    assert rep.ok and [a.worker for a in rep.outcomes["h"].attempts] == ["strong"] and not rep.outcomes["h"].escalated


async def test_token_budget_stops_task_per_sqlite_log(env):
    repo, store = env
    ex = make_exec(repo, lambda tid, w, n: {f"test_{tid}.py": src_test(tid)})  # always a failure
    opts = RunOptions(max_failures=5, token_budget=200)  # each attempt is 110 tokens
    rep = await run_plan(plan_of(task("a")), CFG, repo, store, opts, executor=ex)
    o = rep.outcomes["a"]
    assert o.status == "over_budget" and len(o.attempts) == 2 and not o.escalated
    assert store.usage_for_task(rep.run_id, "a")["tokens_in"] == 200


async def test_discussion_rounds_limit_and_messages(env):
    repo, store = env
    seen = []

    async def reviewer(t, res):
        seen.append(t.id)
        return Verdict(False, "add docstring")  # never approves

    ex = make_exec(repo, lambda tid, w, n: good(tid))
    rep = await run_plan(plan_of(task("a")), CFG, repo, store, RunOptions(discussion_rounds=2),
                         executor=ex, reviewer=reviewer)
    o = rep.outcomes["a"]
    assert o.status == "done" and o.review_rounds == 2 and o.review_unresolved and len(seen) == 2
    assert len(o.attempts) == 2  # first attempt + one fix after the remarks
    msgs = store.inbox("implementer:a")
    assert [m.body for m in msgs] == ["add docstring", "add docstring"] and msgs[0].thread == "a"
    assert "add docstring" in ex.prompts[1] and "add docstring" not in ex.prompts[0]


async def test_no_discussion_by_default_and_approval_ends_early(env):
    repo, store = env
    calls = []

    async def reviewer(t, res):
        calls.append(1)
        return Verdict(True)

    ex = make_exec(repo, lambda tid, w, n: good(tid))
    rep = await run_plan(plan_of(task("a")), CFG, repo, store, RunOptions(), executor=ex, reviewer=reviewer)
    assert rep.ok and calls == []  # 0 rounds by default
    ex2 = make_exec(repo, lambda tid, w, n: good(tid))
    rep2 = await run_plan(plan_of(task("b")), CFG, repo, store, RunOptions(discussion_rounds=2),
                          executor=ex2, reviewer=reviewer)
    assert rep2.ok and calls == [1] and rep2.outcomes["b"].review_rounds == 1 and not rep2.outcomes["b"].review_unresolved


async def test_merge_conflict_counts_as_failure(env):
    repo, store = env
    # two parallel tasks change the same file differently: the second merge conflicts
    ex = make_exec(repo, lambda tid, w, n: {"app.py": f"def add(a, b):\n    return '{tid}{w}'\n",
                                            f"test_{tid}.py": "def test_x():\n    assert True\n"})
    plan = plan_of(task("x", test=f"{sys.executable} -m pytest -q test_x.py"),
                   task("y", test=f"{sys.executable} -m pytest -q test_y.py"))
    rep = await run_plan(plan, CFG, repo, store, RunOptions(parallel=2, max_failures=1, escalate=False), executor=ex)
    statuses = sorted(o.status for o in rep.outcomes.values())
    assert statuses == ["done", "failed"]
    failed = next(o for o in rep.outcomes.values() if o.status == "failed")
    assert "conflict" in failed.error or "merge_conflict" in failed.error


async def test_fatal_worker_status_aborts_remaining(env):
    repo, store = env

    async def ex(t, p, w, b):
        res = await delegate(CFG, repo, p, worker="fake", base=b)
        res.status, res.error = "insufficient_credits", "no credits"
        return res

    plan = plan_of(task("a"), task("b", ["a"]), task("c", ["b"]))
    rep = await run_plan(plan, CFG, repo, store, RunOptions(parallel=1), executor=ex)
    assert rep.outcomes["a"].status == "failed" and "insufficient_credits" in rep.outcomes["a"].error
    assert len(rep.outcomes["a"].attempts) == 1  # no retry and no escalation
    assert rep.outcomes["b"].status == "blocked" and rep.outcomes["c"].status == "blocked"
