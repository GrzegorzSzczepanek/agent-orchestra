import subprocess
import sys
from pathlib import Path

import pytest
from textual.widgets import Button, DataTable, Input, RichLog

from orchestra.controller import AgentSpec, Controller
from orchestra.render import render_events
from orchestra.store import EventStore
from orchestra.tui import OrchestraApp, PermissionScreen

MOCK = [sys.executable, "-m", "orchestra.testing.mock_agent"]


def log_text(app, sel="#log") -> str:
    widget = app._diff if sel == "#diff" else app._log
    return "\n".join(line.text for line in widget.lines)


async def wait_for(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.1)):
        if cond():
            return
        await pilot.pause(0.1)
    raise AssertionError("warunek nie został spełniony w czasie")


@pytest.fixture
def store(tmp_path):
    s = EventStore(tmp_path / "e.db")
    yield s
    s.close()


def test_render_coalesces_stream_chunks(store: EventStore):
    for t in ("Ala ", "ma ", "kota"):
        store.add_event("a", "agent_message", {"text": t})
    store.add_event("a", "tool_call", {"title": "Zapis", "kind": "edit"})
    store.add_event("a", "prompt_end", {"stop_reason": "end_turn"}, tokens_in=3, tokens_out=4)
    lines = [l.plain for l in render_events(store.events())]
    assert lines == ["Ala ma kota", "⚙ Zapis (edit)", "■ koniec tury (end_turn), tokeny 3/4"]


async def test_app_replays_events_and_table(store: EventStore):
    store.add_event("alpha", "session_start", {"cwd": "/x"}, session="s")
    store.add_event("alpha", "prompt_start", {"text": "zrób coś"}, session="s")
    store.add_event("beta", "session_start", {"cwd": "/y"}, session="s")
    store.add_event("beta", "prompt_end", {"stop_reason": "end_turn"}, tokens_in=1200, tokens_out=300)
    app = OrchestraApp(store, poll_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause(0.3)
        table = app.query_one("#agents", DataTable)
        assert table.row_count == 2
        assert app.selected == "alpha"
        assert "zrób coś" in log_text(app)
        assert table.get_cell("alpha", "status").endswith("working")
        assert table.get_cell("beta", "tin") == "1200" and table.get_cell("beta", "tout") == "300"
        # wybór drugiego agenta przełącza strumień
        await pilot.press("down")
        await pilot.pause(0.2)
        assert app.selected == "beta" and "koniec tury" in log_text(app)
        # nowe zdarzenie dociąga się przez polling
        store.add_event("beta", "agent_message", {"text": "nowa wiadomość"})
        await wait_for(pilot, lambda: "nowa wiadomość" in log_text(app))


async def test_prompt_roundtrip_with_mock_agents(store: EventStore, tmp_path: Path):
    specs = []
    for n in ("alpha", "beta"):
        d = tmp_path / n
        d.mkdir()
        specs.append(AgentSpec(n, MOCK, str(d)))
    app = OrchestraApp(store, Controller(store, specs), poll_s=0.05)
    async with app.run_test(size=(120, 36)) as pilot:
        await wait_for(pilot, lambda: len(app.controller.sessions) == 2)
        await wait_for(pilot, lambda: app.selected == "alpha")
        app.query_one("#prompt", Input).focus()
        await pilot.press(*"hej", "enter")
        await wait_for(pilot, lambda: "echo: hej" in log_text(app))
        t = app.query_one("#agents", DataTable)
        await wait_for(pilot, lambda: t.get_cell("alpha", "tin") == "20")
        assert t.get_cell("alpha", "tout") == "10"
        assert t.get_cell("beta", "tin") == "0"  # drugi agent nie dostał promptu


async def test_prompt_without_selection_uses_first_agent(store: EventStore, tmp_path: Path):
    """Prompt wysłany zanim poll ustawi selected nie może zginąć."""
    d = tmp_path / "alpha"
    d.mkdir()
    app = OrchestraApp(store, Controller(store, [AgentSpec("alpha", MOCK, str(d))]), poll_s=0.05)
    async with app.run_test(size=(120, 36)) as pilot:
        await wait_for(pilot, lambda: len(app.controller.sessions) == 1)
        app.selected = ""
        app.query_one("#prompt", Input).value = "hej"
        app.query_one("#prompt", Input).focus()
        await pilot.press("enter")
        assert app.selected == "alpha"
        await wait_for(pilot, lambda: "echo: hej" in log_text(app))


async def test_permission_modal_allows_write(store: EventStore, tmp_path: Path):
    d = tmp_path / "wt"
    d.mkdir()
    app = OrchestraApp(store, Controller(store, [AgentSpec("alpha", MOCK, str(d))]), poll_s=0.05)
    async with app.run_test(size=(120, 36)) as pilot:
        await wait_for(pilot, lambda: "alpha" in app.controller.sessions)
        app.query_one("#prompt", Input).focus()
        app.query_one("#prompt", Input).value = "write:a.txt:treść"
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, PermissionScreen))
        assert "Write a.txt" in str(app.screen.query_one("#perm-title").render())
        await pilot.click("#opt0")
        await wait_for(pilot, lambda: "saved a.txt" in log_text(app))
        assert (d / "a.txt").read_text() == "treść"


async def test_permission_modal_deny(store: EventStore, tmp_path: Path):
    d = tmp_path / "wt"
    d.mkdir()
    app = OrchestraApp(store, Controller(store, [AgentSpec("alpha", MOCK, str(d))]), poll_s=0.05)
    async with app.run_test(size=(120, 36)) as pilot:
        await wait_for(pilot, lambda: "alpha" in app.controller.sessions)
        app.query_one("#prompt", Input).value = "write:b.txt:x"
        app.query_one("#prompt", Input).focus()
        await pilot.press("enter")
        await wait_for(pilot, lambda: isinstance(app.screen, PermissionScreen))
        await pilot.press("escape")
        await wait_for(pilot, lambda: "write refused" in log_text(app))
        assert not (d / "b.txt").exists()


async def test_diff_view(store: EventStore, git_repo: Path):
    (git_repo / "app.py").write_text("def add(a, b):\n    return a + b\n")
    (git_repo / "new.txt").write_text("nowy\n")
    app = OrchestraApp(store, Controller(store, [AgentSpec("alpha", MOCK, str(git_repo))]), poll_s=0.05)
    async with app.run_test(size=(120, 36)) as pilot:
        await wait_for(pilot, lambda: app.selected == "alpha")
        await pilot.press("f2")
        await wait_for(pilot, lambda: "+    return a + b" in log_text(app, "#diff"))
        assert "new.txt" in log_text(app, "#diff")
        await pilot.press("f2")
        assert app.query_one("#diff").display is False


async def test_single_tab_from_table_reaches_prompt(store: EventStore):
    """Regresja z prawdziwego tmux: potrzeba było dwóch Tabów, bo fokus przystawał na RichLog."""
    store.add_event("alpha", "agent_message", {"text": "x"})
    app = OrchestraApp(store, poll_s=0.05)
    async with app.run_test() as pilot:
        await pilot.pause(0.2)
        assert isinstance(app.focused, DataTable)
        await pilot.press("tab")
        assert isinstance(app.focused, Input)


async def test_new_agent_from_tui(store: EventStore, git_repo: Path):
    from orchestra.config import AgentConfig, Config

    cfg = Config(agents={"mock": AgentConfig(name="mock", acp=tuple(MOCK))}, roles={})
    ctrl = Controller(store, [])
    app = OrchestraApp(store, ctrl, poll_s=0.05, config=cfg, repo=git_repo)
    async with app.run_test(size=(120, 36)) as pilot:
        await pilot.press("ctrl+n")
        await pilot.pause(0.2)
        screen = app.screen
        screen.query_one("#new-name", Input).value = "api"
        await pilot.click("#kind0")
        await wait_for(pilot, lambda: "api" in ctrl.sessions)
        await wait_for(pilot, lambda: "api" in app._status)
        assert (Path(ctrl.cwds["api"]) / "app.py").exists()  # własny worktree
        # drugi agent tego samego typu dostaje inną nazwę
        await pilot.press("ctrl+n")
        await pilot.pause(0.2)
        await pilot.click("#kind0")
        await wait_for(pilot, lambda: "mock" in ctrl.sessions)
        # prompt do ręcznie dodanego agenta
        app.select_agent("api")
        app.query_one("#prompt", Input).focus()
        app.query_one("#prompt", Input).value = "napisz coś"
        await pilot.press("enter")
        await wait_for(pilot, lambda: "napisz coś" in log_text(app))


# --- interfejs bez uczenia się komend: tabela zadań, przyciski, wiadomości agent <-> agent ---
from textual.widgets import Select  # noqa: E402

from orchestra.tui import ConfirmScreen, MessageScreen, TaskActionScreen  # noqa: E402


def seed_run(store: EventStore) -> None:
    store.add_event("scheduler", "run_start", {
        "run_id": "r1", "goal": "g", "tasks": 2, "parallel": 2, "integration_branch": "orchestra/run-r1",
        "plan": [{"id": "a", "difficulty": "easy", "depends_on": [], "description": "A"},
                 {"id": "b", "difficulty": "easy", "depends_on": ["a"], "description": "B"}]})
    store.add_event("scheduler", "task_start", {"run_id": "r1", "task_id": "a"})
    store.add_event("task:a", "worker_step", {"run_id": "r1", "task_id": "a", "step": "tool", "text": "write_file: a.py",
                                              "attempt": 1, "worker": "cheap"})


async def test_tasks_visible_immediately_with_live_steps(store: EventStore):
    seed_run(store)
    app = OrchestraApp(store, poll_s=0.05)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        t = app.query_one("#agents", DataTable)
        keys = [k.value for k in t.rows]
        assert "task:a" in keys and "task:b" in keys and "scheduler" in keys
        assert not any(k.startswith(("worker:", "role:")) for k in keys)
        assert t.get_cell("task:a", "status").endswith("working") and t.get_cell("task:b", "status").endswith("pending")
        assert "przebieg r1" in app.sub_title
        app.select_agent("task:a")
        assert "zapisuje a.py" in log_text(app)
        # nowy krok workera pojawia się na żywo w otwartym zadaniu
        store.add_event("task:a", "worker_step", {"run_id": "r1", "task_id": "a", "step": "result", "text": "3 passed in 0.1s",
                                                  "attempt": 1, "worker": "cheap"})
        await wait_for(pilot, lambda: "3 passed" in log_text(app))


async def test_text_on_task_row_is_hint_and_buttons_open_dialogs(store: EventStore):
    seed_run(store)
    app = OrchestraApp(store, poll_s=0.05)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert {b.id for b in app.query("#toolbar Button")} >= {"b-new", "b-msg", "b-hint", "b-detail", "b-cancel", "b-diff"}
        app.select_agent("task:b")
        assert "Wskazówka dla zadania b" in app._prompt.placeholder
        app._prompt.focus()
        app._prompt.value = "użyj foo"
        await pilot.press("enter")
        await pilot.pause(0.1)
        assert store.inbox("task:r1:b")[0].body == "użyj foo"
        await pilot.click("#b-hint")
        await pilot.pause(0.2)
        assert isinstance(app.screen, TaskActionScreen)
        assert app.screen.query_one("#act-task", Select).value == "b"  # zaznaczone zadanie jest domyślne
        await pilot.press("escape")
        await pilot.click("#b-cancel")
        await pilot.pause(0.2)
        assert isinstance(app.screen, ConfirmScreen)
        await pilot.click("#yes")
        await pilot.pause(0.2)
        assert store.inbox("run:r1")[0].body == "cancel"


async def test_message_button_reaches_live_agent_and_agents_talk(store: EventStore, tmp_path: Path):
    specs = []
    for name in ("alpha", "beta"):
        d = tmp_path / name
        d.mkdir()
        specs.append(AgentSpec(name, MOCK, str(d)))
    app = OrchestraApp(store, Controller(store, specs), poll_s=0.05)
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(pilot, lambda: len(app.controller.sessions) == 2)
        app.select_agent("beta")
        await pilot.click("#b-msg")
        await pilot.pause(0.2)
        assert isinstance(app.screen, MessageScreen)
        assert app.screen.query_one("#msg-to", Select).value == "beta"
        app.screen.query_one("#msg-text", Input).value = "cześć"
        await pilot.click("#msg-ok")
        await wait_for(pilot, lambda: "Wiadomość od user: cześć" in log_text(app))
        # agent -> agent (tak działa post_message z narzędzia MCP): adresat dostaje to jako prompt
        store.post_message("beta", "alpha", "odpowiedź od bety")
        app.select_agent("alpha")
        await wait_for(pilot, lambda: "Wiadomość od beta: odpowiedź od bety" in log_text(app))
        # rozmowa jest widoczna w wierszu wiadomości
        assert "inbox" in [k.value for k in app.query_one("#agents", DataTable).rows]


async def test_agent_to_agent_relay_is_capped(store: EventStore, tmp_path: Path):
    d = tmp_path / "a"
    d.mkdir()
    app = OrchestraApp(store, Controller(store, [AgentSpec("alpha", MOCK, str(d))]), poll_s=0.05)
    async with app.run_test(size=(140, 40)) as pilot:
        await wait_for(pilot, lambda: "alpha" in app.controller.sessions)
        for i in range(15):
            store.post_message("beta", "alpha", f"ping{i}")
        await pilot.pause(0.5)
        app.select_agent("alpha")
        text = log_text(app)
        assert "ping9" in text and "ping10" not in text  # po MAX_RELAY człowiek musi się odezwać
