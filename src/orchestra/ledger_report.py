"""Raport rejestru delegacji: tabela i proponowany próg opłacalności (czyste funkcje, bez I/O)."""

from __future__ import annotations

from dataclasses import dataclass

MIN_ROWS = 10  # tyle delegacji potrzeba, żeby próg nie był tylko wstępny


@dataclass(frozen=True)
class LedgerRow:
    task_id: str
    outcome: str  # accepted (bez poprawek) | fixed (z poprawkami) | rejected (odrzucone, zrobione samemu)
    worker_tokens_in: int
    worker_tokens_out: int
    worker_usd: float
    spec_tokens: int  # szacunek: tokeny, które mocny model poświęcił na opis i testy zadania
    review_tokens: int  # szacunek: tokeny, które mocny model poświęcił na recenzję diffu
    diff_tokens: int  # rozmiar diffu workera, przybliżenie kosztu napisania samemu


@dataclass(frozen=True)
class ThresholdProposal:
    n: int
    acceptance_rate: float | None  # średnia z wag: accepted 1.0, fixed 0.5, rejected 0.0
    mean_overhead: float | None  # średnia z spec_tokens + review_tokens
    threshold_tokens: int | None  # delegować, gdy szacowany diff przekracza tyle tokenów
    text: str


def format_table(rows: list[LedgerRow]) -> str:
    """Tabela tekstowa delegacji z wierszem sumy. Szczegóły zachowania opisuje tests/test_ledger_report.py."""
    raise NotImplementedError


def proposed_threshold(rows: list[LedgerRow]) -> ThresholdProposal:
    """Próg opłacalności: średni narzut (opis + recenzja) podzielony przez wskaźnik akceptacji."""
    raise NotImplementedError
