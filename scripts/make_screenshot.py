"""Generuje zrzuty SVG TUI na agentach mock: docs/screenshot.svg i docs/screenshot-permission.svg."""

import asyncio
import sys
import tempfile
from pathlib import Path

from textual.widgets import Input

from orchestra.controller import AgentSpec, Controller
from orchestra.store import EventStore
from orchestra.tui import OrchestraApp, PermissionScreen

MOCK = [sys.executable, "-m", "orchestra.testing.mock_agent"]
DOCS = Path(__file__).resolve().parent.parent / "docs"


async def until(pilot, cond):
    for _ in range(100):
        if cond():
            return
        await pilot.pause(0.1)
    raise TimeoutError


async def main() -> None:
    tmp = Path(tempfile.mkdtemp())
    store = EventStore(tmp / "e.db")
    specs = []
    for n in ("claude", "commandcode"):
        (tmp / n).mkdir()
        specs.append(AgentSpec(n, MOCK, str(tmp / n)))
    app = OrchestraApp(store, Controller(store, specs), poll_s=0.05)
    async with app.run_test(size=(120, 30)) as pilot:
        await until(pilot, lambda: len(app.controller.sessions) == 2)
        box = app.query_one("#prompt", Input)
        box.focus()
        box.value = "zaplanuj moduł logowania"
        await pilot.press("enter")
        await until(pilot, lambda: "echo: zaplanuj" in "".join(l.text for l in app.query_one("#log").lines))
        app.query_one("#agents").focus()
        await pilot.press("down")
        await pilot.pause(0.2)
        box.focus()
        box.value = "write:login.py:print('ok')"
        await pilot.press("enter")
        await until(pilot, lambda: isinstance(app.screen, PermissionScreen))
        await pilot.pause(0.2)
        (DOCS / "screenshot-permission.svg").write_text(app.export_screenshot(title="agent-orchestra"))
        await pilot.click("#opt0")
        await until(pilot, lambda: "zapisano" in "".join(l.text for l in app.query_one("#log").lines))
        await pilot.pause(0.5)
        (DOCS / "screenshot.svg").write_text(app.export_screenshot(title="agent-orchestra"))


asyncio.run(main())
