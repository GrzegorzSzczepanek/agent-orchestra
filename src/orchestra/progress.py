"""Run view (run_plan) built from the event log: shared by the MCP run_status tool and `orchestra watch`."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .store import Event


@dataclass
class TaskView:
    id: str
    difficulty: str = ""
    depends_on: list[str] = field(default_factory=list)
    description: str = ""
    status: str = "pending"  # pending | running | done | failed | blocked | skipped | over_budget
    attempts: int = 0
    worker: str = ""
    last_attempt: str = ""  # status of the worker's last attempt (ok, tests_failed, ...)
    escalated: bool = False
    tokens_in: int = 0
    tokens_out: int = 0
    started: float = 0.0
    ended: float = 0.0


@dataclass
class RunView:
    run_id: str
    goal: str = ""
    integration_branch: str = ""
    started: float = 0.0
    ended: float = 0.0
    finished: bool = False
    ok: bool = False
    tasks: dict[str, TaskView] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["tasks"] = list(d["tasks"].values())
        return d


def build_runs(events: list[Event]) -> dict[str, RunView]:
    runs: dict[str, RunView] = {}
    for ev in events:
        p = ev.payload
        rid = p.get("run_id")
        if not rid or ev.kind not in ("run_start", "task_start", "task_attempt", "task_end", "run_end"):
            continue
        run = runs.setdefault(rid, RunView(rid))
        match ev.kind:
            case "run_start":
                run.goal, run.integration_branch, run.started = p.get("goal", ""), p.get("integration_branch", ""), ev.ts
                for t in p.get("plan", []):
                    run.tasks[t["id"]] = TaskView(t["id"], t.get("difficulty", ""), t.get("depends_on", []),
                                                  t.get("description", ""))
            case "run_end":
                run.finished, run.ok, run.ended = True, bool(p.get("ok")), ev.ts
            case "task_start":
                t = run.tasks.setdefault(p["task_id"], TaskView(p["task_id"]))
                t.status, t.started = "running", ev.ts
            case "task_attempt":
                t = run.tasks.setdefault(p["task_id"], TaskView(p["task_id"]))
                t.attempts = max(t.attempts, int(p.get("attempt", 0)))
                t.worker = ev.agent.removeprefix("worker:")
                t.last_attempt = p.get("status", "")
                t.escalated = t.escalated or bool(p.get("escalated"))
                t.tokens_in += ev.tokens_in
                t.tokens_out += ev.tokens_out
            case "task_end":
                t = run.tasks.setdefault(p["task_id"], TaskView(p["task_id"]))
                t.status, t.ended = p.get("status", "failed"), ev.ts
                t.escalated = t.escalated or bool(p.get("escalated"))
    return runs
