"""Kontrakt dla orchestra.ledger_report (test pisze mocny model, implementuje worker)."""

import re

from orchestra.ledger_report import MIN_ROWS, LedgerRow, ThresholdProposal, format_table, proposed_threshold


def row(task_id, outcome, spec=100, review=100, diff=500, tin=1000, tout=100, usd=0.0123):
    return LedgerRow(task_id, outcome, tin, tout, usd, spec, review, diff)


def cells(line: str) -> list[str]:
    """Komórki wiersza tabeli: kolumny są rozdzielone co najmniej dwiema spacjami."""
    return re.split(r"\s{2,}", line.strip())


HEADER = ["zadanie", "wynik", "worker_in", "worker_out", "worker_usd", "opis", "recenzja", "diff"]


def test_table_empty():
    assert format_table([]) == "Brak zapisanych delegacji."


def test_table_header_rows_and_totals():
    rows = [row("alfa", "accepted", spec=300, review=100, diff=900, tin=2000, tout=200, usd=0.5),
            row("beta", "fixed", spec=200, review=150, diff=400, tin=1000, tout=100, usd=0.25),
            row("gamma", "rejected", spec=100, review=50, diff=0, tin=500, tout=50, usd=0.125)]
    lines = format_table(rows).splitlines()
    assert len(lines) == 5  # nagłówek, 3 wiersze, suma
    assert cells(lines[0]) == HEADER
    assert cells(lines[1]) == ["alfa", "bez poprawek", "2000", "200", "0.5000", "300", "100", "900"]
    assert cells(lines[2]) == ["beta", "z poprawkami", "1000", "100", "0.2500", "200", "150", "400"]
    assert cells(lines[3]) == ["gamma", "odrzucone", "500", "50", "0.1250", "100", "50", "0"]
    assert cells(lines[4]) == ["RAZEM", "1/1/1", "3500", "350", "0.8750", "600", "300", "1300"]


def test_table_totals_count_outcomes_in_order_accepted_fixed_rejected():
    rows = [row("a", "accepted"), row("b", "accepted"), row("c", "rejected")]
    assert cells(format_table(rows).splitlines()[-1])[1] == "2/0/1"


def test_table_columns_are_aligned():
    rows = [row("krotkie", "accepted"), row("bardzo-dlugie-zadanie", "fixed")]
    lines = format_table(rows).splitlines()
    # każda kolumna zaczyna się w tym samym miejscu we wszystkich liniach tabeli (wyrównanie do lewej: zadanie, wynik)
    assert len({line.index(cells(line)[1]) for line in lines[:3]}) == 1


def test_threshold_empty():
    p = proposed_threshold([])
    assert isinstance(p, ThresholdProposal)
    assert (p.n, p.acceptance_rate, p.mean_overhead, p.threshold_tokens) == (0, None, None, None)
    assert "Brak danych" in p.text


def test_threshold_formula_with_preliminary_label():
    rows = [row("a", "accepted", spec=100, review=200), row("b", "accepted", spec=50, review=150),
            row("c", "fixed", spec=100, review=100), row("d", "rejected", spec=80, review=120)]
    p = proposed_threshold(rows)
    # narzut: 300, 200, 200, 200 (średnia 225); wagi: 1, 1, 0.5, 0 (akceptacja 0.625); próg 225 / 0.625 = 360
    assert p.n == 4
    assert p.acceptance_rate == 0.625
    assert p.mean_overhead == 225.0
    assert p.threshold_tokens == 360
    assert "WSTĘPNIE" in p.text and "n=4" in p.text and "360" in p.text


def test_threshold_not_preliminary_from_min_rows():
    assert MIN_ROWS == 10
    rows = [row(f"t{i}", "accepted", spec=60, review=40) for i in range(MIN_ROWS)]
    p = proposed_threshold(rows)
    assert (p.n, p.acceptance_rate, p.mean_overhead, p.threshold_tokens) == (10, 1.0, 100.0, 100)
    assert "WSTĘPNIE" not in p.text and "n=10" in p.text and "100" in p.text


def test_threshold_all_rejected_says_it_does_not_pay_off():
    p = proposed_threshold([row("a", "rejected"), row("b", "rejected")])
    assert p.acceptance_rate == 0.0 and p.threshold_tokens is None
    assert "nie opłaca" in p.text
