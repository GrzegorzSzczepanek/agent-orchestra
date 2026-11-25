"""Delegation ledger and worker session budget.

The budget estimates USD cost from tokens and the agent prices in agents.toml (cmd reports no cost), counted since
the session start saved in `.orchestra/session.json`. The ledger (`orchestra ledger add/show`) puts worker tokens
next to the estimated strong-model tokens for the spec and the review, plus the review outcome.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass
from pathlib import Path

from .config import Config
from .ledger_report import LedgerRow
from .store import EventStore

COST_KINDS = ("task_attempt", "delegate")  # events that carry worker tokens
CHARS_PER_TOKEN = 4  # approximation for text and code, an estimate and not a measurement


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def estimate_cost_usd(tokens_in: int, tokens_out: int, price_in_per_mtok: float, price_out_per_mtok: float) -> float:
    return (tokens_in * price_in_per_mtok + tokens_out * price_out_per_mtok) / 1_000_000


def agent_cost_usd(config: Config, agent: str, tokens_in: int, tokens_out: int) -> float:
    a = config.agents.get(agent)
    return estimate_cost_usd(tokens_in, tokens_out, a.price_in_per_mtok, a.price_out_per_mtok) if a else 0.0


# --- session and budget ---
def session_path(repo: str | Path) -> Path:
    return Path(repo) / ".orchestra" / "session.json"


def start_session(repo: str | Path, limit_usd: float, now: float | None = None) -> None:
    """Starts a new session window: cost counts from now, limit in USD (0 disables the limit)."""
    path = session_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"limit_usd": float(limit_usd), "since": now if now is not None else time.time()}) + "\n",
                    encoding="utf-8")


def read_session(repo: str | Path) -> dict[str, float] | None:
    try:
        data = json.loads(session_path(repo).read_text(encoding="utf-8"))
        return {"limit_usd": float(data["limit_usd"]), "since": float(data["since"])}
    except (OSError, ValueError, KeyError, TypeError):
        return None


@dataclass(frozen=True)
class BudgetState:
    limit_usd: float
    since: float
    spent_usd: float
    unpriced_agents: tuple[str, ...]  # agents with tokens but no price in agents.toml (their cost counts as 0)

    @property
    def exceeded(self) -> bool:
        return self.limit_usd > 0 and self.spent_usd >= self.limit_usd

    def message(self) -> str:
        return (f"worker session budget exceeded: {self.spent_usd:.4f} of {self.limit_usd:.2f} USD "
                "(estimate from prices in agents.toml). Ask the user whether to continue.")


def spent_usd(store: EventStore, config: Config, since: float) -> tuple[float, tuple[str, ...]]:
    total, unpriced = 0.0, set()
    for kind in COST_KINDS:
        for ev in store.events(kind=kind, limit=10_000_000):
            if ev.ts < since or not ev.agent.startswith("worker:"):
                continue
            name = ev.agent.removeprefix("worker:")
            agent = config.agents.get(name)
            if agent is None or not (agent.price_in_per_mtok or agent.price_out_per_mtok):
                if ev.cost <= 0 and (ev.tokens_in or ev.tokens_out) and name != "fake":
                    unpriced.add(name)
            total += ev.cost if ev.cost > 0 else agent_cost_usd(config, name, ev.tokens_in, ev.tokens_out)
    return total, tuple(sorted(unpriced))


def budget_state(repo: str | Path, store: EventStore, config: Config) -> BudgetState | None:
    """Budget state, or None when no session was started (`orchestra ledger budget --set USD`)."""
    sess = read_session(repo)
    if sess is None:
        return None
    spent, unpriced = spent_usd(store, config, sess["since"])
    return BudgetState(sess["limit_usd"], sess["since"], spent, unpriced)


# --- ledger rows ---
def task_worker_usage(store: EventStore, config: Config, run_id: str, task_id: str) -> tuple[str, int, int, float]:
    """Worker agent, token totals and estimated cost of a task's attempts in the given run."""
    worker, tin, tout, cost = "", 0, 0, 0.0
    for ev in store.events(kind="task_attempt", limit=10_000_000):
        if ev.payload.get("run_id") == run_id and ev.payload.get("task_id") == task_id:
            worker = worker or ev.agent.removeprefix("worker:")
            tin, tout = tin + ev.tokens_in, tout + ev.tokens_out
            name = ev.agent.removeprefix("worker:")
            cost += ev.cost if ev.cost > 0 else agent_cost_usd(config, name, ev.tokens_in, ev.tokens_out)
    return worker, tin, tout, cost


def rows_from_store(store: EventStore) -> list[LedgerRow]:
    return [LedgerRow(r["task_id"], r["outcome"], r["worker_tokens_in"], r["worker_tokens_out"], r["worker_usd"],
                      r["spec_tokens"], r["review_tokens"], r["diff_tokens"]) for r in store.delegations()]
