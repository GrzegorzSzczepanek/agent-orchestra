import asyncio

from orchestra.agents_io import REVIEW_PROMPT, make_reviewer
from orchestra.delegate import DelegateResult
from orchestra.plan import PlanTask
from orchestra.scheduler import Verdict


def make_task() -> PlanTask:
    return PlanTask(id="t1", description="dodaj funkcję", acceptance=["a", "b"], test_cmd="pytest -q")


def review(reply: str, task: PlanTask | None = None, result: DelegateResult | None = None) -> Verdict:
    async def ask(prompt: str) -> str:
        return reply

    return asyncio.run(make_reviewer(ask)(task or make_task(), result or DelegateResult(status="ok", diff="+x")))


def test_approve_returns_approved_without_feedback():
    verdict = review("APPROVE")
    assert verdict.approved is True
    assert verdict.feedback == ""


def test_approve_is_case_insensitive_and_ignores_surrounding_whitespace():
    assert review("approve - looks good").approved is True
    assert review("  \n APPROVE \n\t").approved is True


def test_changes_prefix_returns_feedback_without_prefix():
    verdict = review("CHANGES: popraw X")
    assert verdict.approved is False
    assert verdict.feedback == "popraw X"


def test_reply_without_changes_prefix_uses_whole_reply_as_feedback():
    verdict = review("Brakuje testu")
    assert verdict.approved is False
    assert verdict.feedback == "Brakuje testu"


def test_bare_changes_prefix_keeps_whole_reply():
    verdict = review("CHANGES:")
    assert verdict.approved is False
    assert verdict.feedback == "CHANGES:"


def test_prompt_contains_task_details_and_diff():
    captured: dict[str, str] = {}

    async def ask(prompt: str) -> str:
        captured["prompt"] = prompt
        return "APPROVE"

    task = make_task()
    result = DelegateResult(status="ok", diff="+dodana linia diffu")
    asyncio.run(make_reviewer(ask)(task, result))

    prompt = captured["prompt"]
    assert prompt == REVIEW_PROMPT.format(id="t1", description=task.description,
                                          acceptance="- a\n- b", diff=result.diff)
    assert "t1" in prompt
    assert task.description in prompt
    assert "- a" in prompt
    assert "- b" in prompt
    assert result.diff in prompt


def test_long_diff_is_truncated_to_last_12000_chars():
    captured: dict[str, str] = {}

    async def ask(prompt: str) -> str:
        captured["prompt"] = prompt
        return "APPROVE"

    start_marker = "POCZATEK_DIFFU_ZNACZNIK"
    end_marker = "KONIEC_DIFFU_ZNACZNIK"
    body = "x" * (20000 - len(start_marker) - len(end_marker))
    diff = start_marker + body + end_marker
    assert len(diff) == 20000

    asyncio.run(make_reviewer(ask)(make_task(), DelegateResult(status="ok", diff=diff)))

    prompt = captured["prompt"]
    assert end_marker in prompt
    assert start_marker not in prompt
    # ostatnie 12000 znaków diffu jest w promptcie w całości
    assert diff[-12000:] in prompt


def test_parse_verdict_accepts_approve_after_preamble_and_changes_wins():
    from orchestra.agents_io import parse_verdict

    assert parse_verdict("APPROVE").approved
    assert parse_verdict("approve, wygląda dobrze").approved
    assert parse_verdict("Sprawdzam statycznie, każda asercja pasuje.\n\n**APPROVE**").approved  # wstęp przed werdyktem
    v = parse_verdict("Wstęp.\nCHANGES: dodaj test dla pustej listy")
    assert not v.approved and v.feedback == "dodaj test dla pustej listy"
    assert not parse_verdict("CHANGES: APPROVE po poprawkach").approved
    assert not parse_verdict("Nie jestem pewien.").approved
