"""Pasting a block of text into the TUI must keep every line (textual's Input keeps only the first one)."""

from pathlib import Path

from textual import events
from textual.widgets import TextArea

from orchestra.controller import AgentSpec, Controller
from orchestra.store import EventStore
from orchestra.tui import MessageScreen, OrchestraApp, PromptArea

import sys

MOCK = [sys.executable, "-m", "orchestra.testing.mock_agent"]
BLOCK = "first line\n    second line\n\nfourth line"


async def wait_for(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.1)):
        if cond():
            return
        await pilot.pause(0.1)
    raise AssertionError("condition not met in time")


async def test_pasted_block_is_kept_and_sent_with_enter(tmp_path: Path):
    store = EventStore(":memory:")
    app = OrchestraApp(store, Controller(store, [AgentSpec("alpha", MOCK, str(tmp_path))]), poll_s=0.05)
    async with app.run_test(size=(120, 40)) as pilot:
        await wait_for(pilot, lambda: "alpha" in app.controller.sessions)
        prompt = app.query_one("#prompt", PromptArea)
        assert isinstance(prompt, TextArea)
        prompt.focus()
        prompt.post_message(events.Paste(BLOCK))
        await pilot.pause(0.2)
        assert prompt.value == BLOCK
        assert not store.events(kind="prompt_start")  # pasting alone sends nothing
        await pilot.press("enter")
        await wait_for(pilot, lambda: bool(store.events(kind="prompt_start")))
        assert store.events(kind="prompt_start")[0].payload["text"] == BLOCK
        assert prompt.value == ""


async def test_ctrl_j_adds_a_new_line(tmp_path: Path):
    app = OrchestraApp(EventStore(":memory:"), poll_s=0.05)
    async with app.run_test(size=(120, 40)) as pilot:
        prompt = app.query_one("#prompt", PromptArea)
        prompt.focus()
        await pilot.press("a", "ctrl+j", "b")
        assert prompt.value == "a\nb"


async def test_message_dialog_accepts_a_block(tmp_path: Path):
    store = EventStore(":memory:")
    app = OrchestraApp(store, poll_s=0.05)
    async with app.run_test(size=(120, 40)) as pilot:
        sent = []
        app.push_screen(MessageScreen(["alpha"]), callback=sent.append)
        await pilot.pause(0.2)
        field = app.screen.query_one("#msg-text", PromptArea)
        field.focus()
        field.post_message(events.Paste(BLOCK))
        await pilot.pause(0.2)
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert sent == [("alpha", BLOCK)]
