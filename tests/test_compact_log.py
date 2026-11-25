"""Compact worker log view (default in the TUI) and the F9 toggle."""

from pathlib import Path

from textual.widgets import RichLog

from orchestra.controller import Controller
from orchestra.render import render_events
from orchestra.store import Event, EventStore
from orchestra.tui import OrchestraApp
from orchestra.watch import fmt_event

WT = "/Users/x/repo/.orchestra/worktrees/task-abc123"


def ev(kind, payload):
    return Event(1, 0.0, "task:a", "", kind, payload, 15000, 100, 0.0)


def step(kind, text, compact=True):
    line = fmt_event(ev("worker_step", {"step": kind, "text": text}), compact)
    return None if line is None else line.plain


def test_compact_hides_thinking_and_file_dumps():
    assert step("think", "considering a regex") is None
    assert step("result", "src/a.py-130- if line.strip(): src/a.py-131- return error") is None
    assert fmt_event(ev("worker_end", {"status": "ok"}), True) is None
    assert render_events([ev("agent_thought", {"text": "hmm"})], compact=True) == []


def test_compact_shortens_paths_and_names_actions():
    assert step("tool", f"write_file: {WT}/tests/test_x.py").strip() == "writes tests/test_x.py"
    assert step("tool", "shell_command: /Users/x/repo/.venv/bin/pytest -q tests/test_x.py").strip() == "$ pytest -q tests/test_x.py"
    assert step("tool", "new_tool: x").strip() == "new_tool x"


def test_compact_keeps_test_outcomes_and_clips_long_text():
    assert step("result", "........ [100%] 40 passed in 0.25s").strip() == "↳ 40 passed in 0.25s"
    assert "2 failed" in step("result", "F.F [100%] 2 failed, 1 passed in 0.1s")
    assert len(step("text", "a" * 1000).strip()) == 240


def test_compact_attempt_shows_reason_only_on_failure():
    ok = fmt_event(ev("task_attempt", {"attempt": 1, "status": "ok"}), True).plain
    assert "attempt 1: ok, 15k tok" in ok and "F5" not in ok
    bad = fmt_event(ev("task_attempt", {"attempt": 2, "status": "tests_failed",
                                        "test_output": "\n".join(f"line {i}" for i in range(20))}), True).plain
    assert "line 19" in bad and "line 10" not in bad and "F5" in bad


def test_full_view_is_unchanged():
    assert step("think", "considering", compact=False) is not None
    assert WT in step("tool", f"write_file: {WT}/a.py", compact=False)


async def test_f9_toggles_full_log(git_repo: Path):
    store.add_event("task:a", "worker_step", {"step": "think", "text": "SECRET-REASONING", "task_id": "a"})
    store.add_event("task:a", "worker_step", {"step": "think", "text": "TAJNE-ROZUMOWANIE", "task_id": "a"})
    app = OrchestraApp(store, Controller(store, []), poll_s=0.05)
    async with app.run_test(size=(100, 36)) as pilot:
        await pilot.pause(0.3)
        app.select_agent("task:a")

        def shown() -> str:
            return "\n".join(line.text for line in app.query_one("#log", RichLog).lines)
assert "SECRET-REASONING" not in shown()
        await pilot.pause(0.2)
        assert "TAJNE-ROZUMOWANIE" not in shown()
        assert "SECRET-REASONING" in shown()
        await pilot.pause(0.2)
        assert "TAJNE-ROZUMOWANIE" in shown()
