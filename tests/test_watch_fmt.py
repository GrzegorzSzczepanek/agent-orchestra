import time

from orchestra.store import Event
from orchestra.watch import fmt_event

TS = 1700000000.0
CLOCK = time.strftime("%H:%M:%S", time.localtime(TS))


def ev(kind, payload, *, agent="scheduler", tokens_in=0, tokens_out=0):
    return Event(1, TS, agent, "", kind, payload, tokens_in, tokens_out, 0.0)


def test_run_start():
    line = fmt_event(ev("run_start", {"run_id": "r1", "tasks": 3, "parallel": 2}))
    assert line.plain == f"{CLOCK} ▶ run r1: 3 tasks, parallel 2"
    assert line.style == "bold cyan"


def test_task_start():
    line = fmt_event(ev("task_start", {"task_id": "a", "difficulty": "easy"}))
    assert "●" in line.plain and "a start" in line.plain and "(easy)" in line.plain
    assert line.style == "yellow"


def test_worker_step_markers_and_styles():
    cases = {
        "tool": ("⚙", "yellow"),
        "result": ("↳", "dim"),
        "text": ("💬", "green"),
        "think": ("…", "dim italic"),
        "other": ("·", ""),
    }
    for step, (mark, style) in cases.items():
        line = fmt_event(ev("worker_step", {"step": step, "text": "payload"}, agent="task:a"))
        assert mark in line.plain and "payload" in line.plain
        assert line.style == style


def test_worker_end_ok_and_fail():
    ok = fmt_event(ev("worker_end", {"attempt": 2, "status": "ok"}, agent="task:a"))
    assert ok.plain == f"{CLOCK} ■ attempt 2: ok" and ok.style == "green"
    bad = fmt_event(ev("worker_end", {"attempt": 1, "status": "tests_failed"}, agent="task:a"))
    assert "tests_failed" in bad.plain and bad.style == "red"


def test_message():
    line = fmt_event(ev("message", {"from": "reviewer", "to": "implementer:a", "body": "APPROVE"}, agent="inbox"))
    assert line.plain == f"{CLOCK} ✉ reviewer → implementer:a: APPROVE"
    assert line.style == "bold blue"


def test_message_body_truncated_to_400():
    line = fmt_event(ev("message", {"from": "x", "to": "y", "body": "z" * 500}))
    assert line.plain.endswith("z" * 400) and "z" * 401 not in line.plain


def test_hint_applied():
    line = fmt_event(ev("hint_applied", {"task_id": "b", "text": "check the import"}))
    assert line.plain == f"{CLOCK} ✎ hint for b: check the import"
    assert line.style == "bold magenta"


def test_run_cancel():
    line = fmt_event(ev("run_cancel", {"by": "tui"}))
    assert line.plain == f"{CLOCK} ✗ run cancelled (tui)"
    assert line.style == "bold red"


def test_unknown_kind_returns_none():
    assert fmt_event(ev("agent_message", {"text": "x"}, agent="claude")) is None
