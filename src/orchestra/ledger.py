"""Rejestr delegacji i budżet sesji workerów.

Budżet liczy szacowany koszt USD z tokenów i cen agenta z agents.toml (cmd nie raportuje kosztu), od chwili
startu sesji zapisanej w `.orchestra/session.json`. Rejestr (`orchestra ledger add/show`) zestawia tokeny workera
z szacunkiem tokenów mocnego modelu na opis i recenzję oraz wynik recenzji.
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

COST_KINDS = ("task_attempt", "delegate")  # zdarzenia z tokenami workera
CHARS_PER_TOKEN = 4  # przybliżenie dla tekstu i kodu, to szacunek, nie pomiar


def estimate_tokens(text: str) -> int:
    return math.ceil(len(text) / CHARS_PER_TOKEN) if text else 0


def estimate_cost_usd(tokens_in: int, tokens_out: int, price_in_per_mtok: float, price_out_per_mtok: float) -> float:
    return (tokens_in * price_in_per_mtok + tokens_out * price_out_per_mtok) / 1_000_000


def agent_cost_usd(config: Config, agent: str, tokens_in: int, tokens_out: int) -> float:
    a = config.agents.get(agent)
    return estimate_cost_usd(tokens_in, tokens_out, a.price_in_per_mtok, a.price_out_per_mtok) if a else 0.0


# --- sesja i budżet ---
def session_path(repo: str | Path) -> Path:
    return Path(repo) / ".orchestra" / "session.json"


def start_session(repo: str | Path, limit_usd: float, now: float | None = None) -> None:
    """Zaczyna nowe okno sesji: koszt liczy się od teraz, limit w USD (0 wyłącza limit)."""
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
    unpriced_agents: tuple[str, ...]  # agenci z tokenami, ale bez ceny w agents.toml (ich koszt liczy się jako 0)

    @property
    def exceeded(self) -> bool:
        return self.limit_usd > 0 and self.spent_usd >= self.limit_usd

    def message(self) -> str:
        return (f"budżet sesji workerów przekroczony: {self.spent_usd:.4f} z {self.limit_usd:.2f} USD "
                "(szacunek z cen w agents.toml). Zapytaj użytkownika, czy kontynuować.")


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
    """Stan budżetu albo None, gdy sesja nie została rozpoczęta (`orchestra ledger budget --set USD`)."""
    sess = read_session(repo)
    if sess is None:
        return None
    spent, unpriced = spent_usd(store, config, sess["since"])
    return BudgetState(sess["limit_usd"], sess["since"], spent, unpriced)


# --- wiersze rejestru ---
def task_worker_usage(store: EventStore, config: Config, run_id: str, task_id: str) -> tuple[str, int, int, float]:
    """Agent workera i suma tokenów oraz szacowany koszt prób zadania w danym przebiegu."""
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
