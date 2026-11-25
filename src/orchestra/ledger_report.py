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


WEIGHTS = {"accepted": 1.0, "fixed": 0.5, "rejected": 0.0}


def format_table(rows: list[LedgerRow]) -> str:
    """Text table of delegations with a totals row. Behaviour is specified in tests/test_ledger_report.py."""
    if not rows:
        return "No delegations recorded."

    header = ["task", "outcome", "worker_in", "worker_out", "worker_usd", "spec", "review", "diff"]
    body = [
        [r.task_id, r.outcome, str(r.worker_tokens_in), str(r.worker_tokens_out),
         f"{r.worker_usd:.4f}", str(r.spec_tokens), str(r.review_tokens), str(r.diff_tokens)]
        for r in rows
    ]
    counts = {outcome: 0 for outcome in WEIGHTS}
    for r in rows:
        counts[r.outcome] = counts.get(r.outcome, 0) + 1
    total = [
        "TOTAL",
        f"{counts['accepted']}/{counts['fixed']}/{counts['rejected']}",
        str(sum(r.worker_tokens_in for r in rows)),
        str(sum(r.worker_tokens_out for r in rows)),
        f"{sum(r.worker_usd for r in rows):.4f}",
        str(sum(r.spec_tokens for r in rows)),
        str(sum(r.review_tokens for r in rows)),
        str(sum(r.diff_tokens for r in rows)),
    ]
    table = [header, *body, total]
    widths = [max(len(line[i]) for line in table) for i in range(len(header))]
    return "\n".join(
        "  ".join(cell.ljust(width) for cell, width in zip(line[:-1], widths[:-1])) + "  " + line[-1]
        for line in table
    )


def proposed_threshold(rows: list[LedgerRow]) -> ThresholdProposal:
    """Break-even threshold: mean overhead (spec + review) divided by the acceptance rate."""
    n = len(rows)
    if n == 0:
        return ThresholdProposal(0, None, None, None, "No data: no delegations recorded yet.")

    acceptance_rate = sum(WEIGHTS.get(r.outcome, 0.0) for r in rows) / n
    mean_overhead = sum(r.spec_tokens + r.review_tokens for r in rows) / n

    if acceptance_rate == 0.0:
        return ThresholdProposal(
            n, 0.0, mean_overhead, None,
            f"No delegation paid off (n={n}): delegation does not pay off at this acceptance rate.",
        )

    threshold_tokens = int(round(mean_overhead / acceptance_rate))
    if n < MIN_ROWS:
        text = (
            f"PRELIMINARY: delegate when the estimated diff exceeds {threshold_tokens} tokens "
            f"(n={n}, fewer than {MIN_ROWS} delegations)."
        )
    else:
        text = f"Delegate when the estimated diff exceeds {threshold_tokens} tokens (n={n})."
    return ThresholdProposal(n, acceptance_rate, mean_overhead, threshold_tokens, text)
