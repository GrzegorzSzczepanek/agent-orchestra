"""TUI w Textual: lista agentów, strumień zdarzeń, prompt, podgląd diffu i okno uprawnień."""

from __future__ import annotations

import argparse
import asyncio
import sys
import tempfile
from pathlib import Path
from typing import Any

from acp.schema import PermissionOption, ToolCallUpdate
from rich.syntax import Syntax
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog

from .controller import AgentSpec, Controller
from .render import render_events, status_of
from .store import EventStore
from .worktree import view_diff

STATUS_ICON = {"idle": "○ idle", "working": "● working", "waiting": "? waiting", "error": "✗ error",
               "stopped": "· stopped", "": "· ?"}


class PermissionScreen(ModalScreen[str | None]):
    """Okno akceptacji uprawnień zgłoszonych przez agenta (ACP request_permission)."""

    BINDINGS = [Binding("escape", "deny", "Odmów")]
    DEFAULT_CSS = """
    PermissionScreen { align: center middle; }
    #perm-box { width: 64; height: auto; border: thick $warning; background: $surface; padding: 1 2; }
    #perm-box Button { margin-top: 1; width: 100%; }
    """

    def __init__(self, agent: str, title: str, options: list[PermissionOption]):
        super().__init__()
        self.agent, self.title_text, self.options = agent, title, options

    def compose(self) -> ComposeResult:
        with Vertical(id="perm-box"):
            yield Label(f"Agent [b]{self.agent}[/b] prosi o zgodę:")
            yield Label(self.title_text, id="perm-title", markup=False)
            for i, opt in enumerate(self.options):
                variant = "success" if opt.kind.startswith("allow") else "error"
                yield Button(opt.name, id=f"opt{i}", variant=variant)
            yield Button("Anuluj", id="cancel")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        self.dismiss(self.options[int(bid[3:])].option_id if bid.startswith("opt") else None)

    def action_deny(self) -> None:
        self.dismiss(None)


class OrchestraApp(App[None]):
    TITLE = "agent-orchestra"
    CSS = """
    #side { width: 46; border-right: solid $primary; }
    #main { width: 1fr; }
    #log, #diff { height: 1fr; }
    #diff { display: none; border-top: solid $secondary; }
    #prompt { dock: bottom; }
    """
    BINDINGS = [
        Binding("f2", "toggle_diff", "Diff"),
        Binding("ctrl+q", "quit", "Wyjście"),
    ]

    def __init__(self, store: EventStore, controller: Controller | None = None, poll_s: float = 0.2):
        super().__init__()
        self.store, self.controller, self.poll_s = store, controller, poll_s
        self.selected = ""
        self._last_id = 0
        self._status: dict[str, str] = {}
        if controller is not None:
            controller.permission_handler = self.ask_permission

    # --- układ ---
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal():
            with Vertical(id="side"):
                yield DataTable(id="agents", cursor_type="row")
            with Vertical(id="main"):
                yield RichLog(id="log", wrap=True, highlight=False, markup=False)
                yield RichLog(id="diff", wrap=False, highlight=False, markup=False)
                yield Input(placeholder="Prompt do wybranego agenta (Enter, F2 pokazuje diff)", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        # referencje do widgetów: self.query_one szuka na aktywnym ekranie, a nie gdy na wierzchu jest modal
        self._table = self.query_one("#agents", DataTable)
        self._log = self.query_one("#log", RichLog)
        self._diff = self.query_one("#diff", RichLog)
        table = self._table
        for key, label in (("agent", "agent"), ("status", "status"), ("tin", "in"), ("tout", "out"), ("cost", "$")):
            table.add_column(label, key=key)
        self.set_interval(self.poll_s, self.poll)
        if self.controller is not None:
            self.run_worker(self.controller.start_all(), exclusive=False)
        self.poll()

    async def on_unmount(self) -> None:
        if self.controller is not None:
            await self.controller.aclose()

    # --- polling logu zdarzeń ---
    def poll(self) -> None:
        events = self.store.events(since_id=self._last_id, limit=2000)
        table = self._table
        if events:
            self._last_id = events[-1].id
            for ev in events:
                if ev.agent not in self._status:
                    self._status[ev.agent] = ""
                    table.add_row(ev.agent, "", "0", "0", "0.00", key=ev.agent)
                self._status[ev.agent] = status_of(ev.kind, self._status[ev.agent])
            if not self.selected:
                self.select_agent(self.store.agents()[0])
            else:
                mine = [e for e in events if e.agent == self.selected]
                log = self._log
                for line in render_events(mine):
                    log.write(line)
        for agent, tot in self.store.totals().items():
            if agent in self._status:
                table.update_cell(agent, "status", STATUS_ICON.get(self._status[agent], self._status[agent]), update_width=True)
                table.update_cell(agent, "tin", f"{int(tot['tokens_in'])}", update_width=True)
                table.update_cell(agent, "tout", f"{int(tot['tokens_out'])}", update_width=True)
                table.update_cell(agent, "cost", f"{tot['cost']:.2f}", update_width=True)

    def select_agent(self, name: str) -> None:
        self.selected = name
        self.sub_title = name
        log = self._log
        log.clear()
        for line in render_events(self.store.events(agent=name, limit=5000)):
            log.write(line)

    @on(DataTable.RowHighlighted)
    def row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        name = event.row_key.value if event.row_key is not None else None
        if name and name != self.selected:
            self.select_agent(name)

    # --- prompt ---
    @on(Input.Submitted, "#prompt")
    def submit_prompt(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        event.input.value = ""
        if text and self.selected and self.controller is not None:
            self.run_worker(self.controller.send(self.selected, text), exclusive=False)

    # --- diff ---
    async def action_toggle_diff(self) -> None:
        diff_log = self._diff
        if diff_log.display:
            diff_log.display = False
            return
        diff_log.display = True
        diff_log.clear()
        cwd = self.controller.cwds.get(self.selected) if self.controller else None
        if not cwd:
            diff_log.write("Brak worktree dla wybranego agenta.")
            return
        try:
            text = await view_diff(cwd)
        except Exception as exc:  # noqa: BLE001
            diff_log.write(f"Błąd git: {exc}")
            return
        diff_log.write(Syntax(text, "diff") if text.strip() else "Brak zmian względem HEAD.")

    # --- uprawnienia ---
    async def ask_permission(self, agent: str, tool_call: ToolCallUpdate, options: list[PermissionOption]) -> str | None:
        fut: asyncio.Future[str | None] = asyncio.get_running_loop().create_future()

        def done(result: Any) -> None:
            if not fut.done():
                fut.set_result(result)

        self.push_screen(PermissionScreen(agent, tool_call.title or tool_call.tool_call_id, options), callback=done)
        return await fut


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="orchestra-tui", description="TUI orkiestratora agentów")
    ap.add_argument("--repo", default=".", help="repozytorium, w którym powstaną worktree agentów")
    ap.add_argument("--config", default="agents.toml")
    ap.add_argument("--db", default=None, help="ścieżka bazy zdarzeń (domyślnie <repo>/.orchestra/events.db)")
    ap.add_argument("--agents", default="", help="lista agentów z agents.toml po przecinku")
    ap.add_argument("--demo", action="store_true", help="dwa agenty mock w katalogu tymczasowym")
    args = ap.parse_args(argv)

    repo = Path(args.repo).resolve()
    store = EventStore(args.db or repo / ".orchestra" / "events.db")

    if args.demo:
        tmp = Path(tempfile.mkdtemp(prefix="orchestra-demo-"))
        specs = []
        for name in ("alpha", "beta"):
            d = tmp / name
            d.mkdir()
            specs.append(AgentSpec(name, [sys.executable, "-m", "orchestra.testing.mock_agent"], str(d)))
    else:
        from .config import load_config
        from .controller import specs_from_config

        names = [n for n in args.agents.split(",") if n]
        if not names:
            ap.error("podaj --agents albo --demo")
        specs = asyncio.run(specs_from_config(load_config(args.config), repo, names))

    OrchestraApp(store, Controller(store, specs)).run()


if __name__ == "__main__":
    main()
