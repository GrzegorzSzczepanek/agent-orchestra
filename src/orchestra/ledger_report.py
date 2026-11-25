"""Delegation ledger report: table and a proposed break-even threshold (pure functions, no I/O)."""

from __future__ import annotations

from dataclasses import dataclass

MIN_ROWS = 10  # this many delegations are needed for the threshold to be more than preliminary


@dataclass(frozen=True)
class LedgerRow:
    task_id: str
    outcome: str  # accepted (no fixes) | fixed (with fixes) | rejected (thrown away, done by hand)
    worker_tokens_in: int
    worker_tokens_out: int
    worker_usd: float
    spec_tokens: int  # estimate: tokens the strong model spent on the task description and tests
    review_tokens: int  # estimate: tokens the strong model spent reviewing the diff
    diff_tokens: int  # size of the worker's diff, an approximation of the cost of writing it yourself


@dataclass(frozen=True)
class ThresholdProposal:
    n: int
    acceptance_rate: float | None  # weighted mean: accepted 1.0, fixed 0.5, rejected 0.0
    mean_overhead: float | None  # mean of spec_tokens + review_tokens
    threshold_tokens: int | None  # delegate when the estimated diff exceeds this many tokens
    text: str


def format_table(rows: list[LedgerRow]) -> str:
    """Text table of delegations with a totals row. Behaviour is specified in tests/test_ledger_report.py."""
    raise NotImplementedError


def proposed_threshold(rows: list[LedgerRow]) -> ThresholdProposal:
    """Break-even threshold: mean overhead (spec + review) divided by the acceptance rate."""
    raise NotImplementedError
