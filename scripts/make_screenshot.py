"""Generate TUI screenshots for the README (docs/*.png) from mock agents and a made-up run_plan.

Usage: .venv/bin/python scripts/make_screenshot.py   (PNG requires rsvg-convert, otherwise SVG files remain)
"""

import asyncio
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from orchestra.config import AgentConfig, Config
from orchestra.controller import AgentSpec, Controller
from orchestra.store import EventStore
from orchestra.tui import OrchestraApp
from orchestra.watch import WatchApp

MOCK = [sys.executable, "-m", "orchestra.testing.mock_agent"]
DOCS = Path(__file__).resolve().parent.parent / "docs"
RUN = "a1b2c3d4"


def save(app: OrchestraApp | WatchApp, name: str) -> None:
    svg = DOCS / f"{name}.svg"
    svg.write_text(app.export_screenshot(), encoding="utf-8")
    if shutil.which("rsvg-convert"):
        subprocess.run(["rsvg-convert", "-w", "1600", str(svg), "-o", str(DOCS / f"{name}.png")], check=True)
        svg.unlink()


def step(store: EventStore, task: str, kind: str, text: str) -> None:
    store.add_event(f"task:{task}", "worker_step",
                    {"run_id": RUN, "task_id": task, "attempt": 1, "worker": "commandcode", "step": kind, "text": text})


def seed(store: EventStore) -> None:
    plan = [("slugify", [], "slugify function in text_utils.py"), ("parse_duration", [], "duration parser in durations.py"),
            ("mean", [], "average with validation in stats.py"), ("report", ["slugify", "mean"], "report combining the modules in report.py")]
    store.add_event("scheduler", "run_start", {
        "run_id": RUN, "goal": "Add four small modules with tests", "tasks": 4, "parallel": 2,
        "integration_branch": f"orchestra/run-{RUN}",
        "plan": [{"id": i, "difficulty": "easy", "depends_on": d, "description": t} for i, d, t in plan]})
    for t in ("slugify", "parse_duration"):
        store.add_event("scheduler", "task_start", {"run_id": RUN, "task_id": t, "difficulty": "easy"})
    step(store, "slugify", "tool", "shell_command: ls src tests")
    step(store, "slugify", "result", "text_utils.py  test_text_utils.py")
    step(store, "slugify", "tool", "write_file: src/text_utils.py")
    step(store, "slugify", "result", "File created successfully")
    step(store, "slugify", "tool", "shell_command: pytest -q tests/test_text_utils.py")
    step(store, "slugify", "result", "4 passed in 0.02s")
    store.add_event("worker:commandcode", "task_attempt",
                    {"run_id": RUN, "task_id": "slugify", "attempt": 1, "status": "ok", "files": ["src/text_utils.py"]},
                    tokens_in=58133, tokens_out=689)
    store.add_event("task:slugify", "worker_end", {"run_id": RUN, "task_id": "slugify", "attempt": 1, "status": "ok"})
    store.add_event("scheduler", "task_end", {"run_id": RUN, "task_id": "slugify", "status": "done", "attempts": 1,
                                              "escalated": False, "tokens": 58822})
    store.add_event("scheduler", "task_start", {"run_id": RUN, "task_id": "mean", "difficulty": "easy"})
    store.add_event("worker:commandcode", "task_attempt",
                    {"run_id": RUN, "task_id": "parse_duration", "attempt": 1, "status": "tests_failed", "cwd": "src",
                     "test_cmd": "pytest -q tests/test_durations.py", "files": ["src/durations.py"], "error": "",
                     "test_output": "E   AssertionError: assert parse('1h30m') == 5400\nE    +  where 5400 = 3600\n"
                                    "FAILED tests/test_durations.py::test_hours_minutes"},
                    tokens_in=44871, tokens_out=1204)
    store.add_event("task:parse_duration", "worker_end", {"run_id": RUN, "task_id": "parse_duration", "attempt": 1,
                                                          "status": "tests_failed"})
    step(store, "parse_duration", "tool", "write_file: src/durations.py")
    step(store, "mean", "tool", "write_file: src/stats.py")
    step(store, "mean", "result", "File created successfully")
    store.post_message("reviewer", "implementer:slugify", "APPROVE")
    store.post_message("claude", "commandcode", "Do you have the duration parser yet? I need it in report.py.")
    store.post_message("commandcode", "claude", "Yes, durations.parse_duration, wiring it into report.py after green tests.")


async def main() -> None:
    DOCS.mkdir(exist_ok=True)
    tmp = Path(tempfile.mkdtemp())
    repo = tmp / "demo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "-c", "user.email=d@d", "-c", "user.name=demo", "commit", "-q", "--allow-empty", "-m", "init"],
                   cwd=repo, check=True)
    store = EventStore(tmp / "events.db")
    specs = []
    for n in ("claude", "commandcode"):
        d = tmp / n
        d.mkdir()
        specs.append(AgentSpec(n, MOCK, str(d)))
    demo = {"claude": "strong", "commandcode": "cheap", "opencode": "cheap", "codex": "strong"}
    cfg = Config(agents={k: AgentConfig(name=k, acp=tuple(MOCK), headless=(sys.executable, "{task}"), tier=tier,
                                        default_model="deepseek/deepseek-v4.1-flash" if k == "commandcode" else "")
                         for k, tier in demo.items()},
                 roles={"planner": "claude", "reviewer": "claude", "implementer": "commandcode"})

    app = OrchestraApp(store, Controller(store, specs), poll_s=0.05, config=cfg, repo=repo)
    async with app.run_test(size=(150, 40)) as pilot:
        await pilot.pause(1.0)
        seed(store)
        await pilot.pause(0.8)
        app.select_agent("task:slugify")
        app._table.move_cursor(row=app._table.get_row_index("task:slugify"))
        await pilot.pause(0.3)
        save(app, "tui-overview")

        app.select_agent("task:parse_duration")
        app._table.move_cursor(row=app._table.get_row_index("task:parse_duration"))
        app.show_detail("parse_duration")
        await pilot.pause(0.3)
        save(app, "tui-failure")

        app.select_agent("inbox")
        app._table.move_cursor(row=app._table.get_row_index("inbox"))
        await pilot.pause(0.3)
        save(app, "tui-messages")

        await pilot.press("ctrl+n")
        await pilot.pause(0.4)
        save(app, "tui-new-agent")

    # without Claude: the planner and reviewer roles move to an available agent, the Role and Plan windows
    absent = ("claude", "codex")  # these binaries "do not exist", so the roles move to commandcode
    missing = {k: AgentConfig(name=k, acp=tuple(MOCK), headless=("not-in-path", "{task}") if k in absent
                              else (sys.executable, "{task}"), tier=a.tier, default_model=a.default_model)
               for k, a in cfg.agents.items()}
    cfg2 = Config(agents=missing, roles=dict(cfg.roles), fallback_order=("claude", "commandcode", "opencode", "codex"))
    store2 = EventStore(tmp / "events2.db")
    specs2 = [AgentSpec("commandcode", MOCK, str(tmp / "commandcode"))]
    app2 = OrchestraApp(store2, Controller(store2, specs2), poll_s=0.05, config=cfg2, repo=repo)
    async with app2.run_test(size=(150, 40)) as pilot:
        await pilot.pause(1.0)
        await pilot.press("f8")
        await pilot.pause(0.5)
        save(app2, "tui-roles")
        await pilot.press("escape")
        await pilot.pause(0.3)
        await pilot.press("f7")
        await pilot.pause(0.5)
        app2.screen.query_one("#plan-goal").value = "add e-mail validation with pytest tests"
        await pilot.pause(0.2)
        save(app2, "tui-plan")

    watch = WatchApp(store, poll_s=0.05)
    async with watch.run_test(size=(150, 36)) as pilot:
        await pilot.pause(0.6)
        save(watch, "watch")


if __name__ == "__main__":
    asyncio.run(main())
