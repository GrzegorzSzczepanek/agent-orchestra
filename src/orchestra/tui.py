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

from .config import Config
from .controller import AgentSpec, Controller
from .render import render_events, status_of
from .store import EventStore
from .worktree import view_diff

STATUS_ICON = {"done": "✓ done", "idle": "○ idle", "working": "● working", "waiting": "? waiting", "error": "✗ error",
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


class NewAgentScreen(ModalScreen[tuple[str, str] | None]):
    """Wybór typu agenta (z agents.toml) i opcjonalnej nazwy dla nowej sesji."""

    BINDINGS = [Binding("escape", "cancel", "Anuluj")]
    DEFAULT_CSS = """
    NewAgentScreen { align: center middle; }
    #new-box { width: 64; height: auto; border: thick $success; background: $surface; padding: 1 2; }
    #new-box Button { margin-top: 1; width: 100%; }
    """

    def __init__(self, kinds: list[str]):
        super().__init__()
        self.kinds = kinds

    def compose(self) -> ComposeResult:
        with Vertical(id="new-box"):
            yield Label("Nowy agent: nazwa (opcjonalnie), potem wybierz typ")
            yield Input(placeholder="nazwa, np. api-worker", id="new-name")
            for i, kind in enumerate(self.kinds):
                yield Button(kind, id=f"kind{i}", variant="success")
            yield Button("Anuluj", id="cancel")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        bid = event.button.id or ""
        if bid.startswith("kind"):
            self.dismiss((self.kinds[int(bid[4:])], self.query_one("#new-name", Input).value.strip()))
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
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
        Binding("ctrl+n", "new_agent", "Nowy agent"),
        Binding("ctrl+q", "quit", "Wyjście"),
    ]

    def __init__(self, store: EventStore, controller: Controller | None = None, poll_s: float = 0.2,
                 config: Config | None = None, repo: Path | None = None):
        super().__init__()
        self.store, self.controller, self.poll_s = store, controller, poll_s
        self.config, self.repo = config, repo
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
                yield Input(placeholder="Prompt do agenta · /hint <zadanie> <tekst> · /cancel · /detail <zadanie> · /help", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        # referencje do widgetów: self.query_one szuka na aktywnym ekranie, a nie gdy na wierzchu jest modal
        self._table = self.query_one("#agents", DataTable)
        self._log = self.query_one("#log", RichLog)
        self._diff = self.query_one("#diff", RichLog)
        # Tab ma iść z tabeli prosto do promptu (logi przewijasz myszą), bez przystanku na RichLog
        self._log.can_focus = False
        self._diff.can_focus = False
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
                if ev.kind == "worker_end" and ev.payload.get("status") != "ok":
                    self._status[ev.agent] = "error"
                if ev.kind == "task_end":  # końcowy stan wiersza task:<id>
                    row = f"task:{ev.payload.get('task_id')}"
                    if row in self._status:
                        self._status[row] = "done" if ev.payload.get("status") == "done" else "error"
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
        if text.startswith("/"):
            self.run_command(text)
            return
        if not text or self.controller is None:
            return
        if not self.selected:  # prompt przed pierwszym poll: nie gub tekstu, wybierz pierwszego agenta
            agents = self.store.agents()
            if not agents:
                return
            self.select_agent(agents[0])
        self.run_worker(self.controller.send(self.selected, text), exclusive=False)

    # --- polecenia sterujące przebiegiem (run_plan) ---
    def _active_run(self):
        from .progress import build_runs

        runs = list(build_runs(self.store.events(agent="scheduler", limit=100000)).values())
        active = [r for r in runs if not r.finished]
        return (active or runs or [None])[-1]

    def run_command(self, text: str) -> None:
        cmd, _, rest = text.partition(" ")
        run = self._active_run()
        if cmd == "/help":
            self._log.write("Polecenia: /hint <zadanie> <tekst> (wskazówka na następną próbę), /cancel (anuluj przebieg), "
                            "/detail <zadanie> (błąd, wynik testów, pliki ostatniej próby)")
            return
        if run is None:
            self.notify("Brak przebiegu run_plan w logu", severity="warning")
            return
        if cmd == "/cancel":
            if run.finished:
                self.notify(f"Przebieg {run.run_id} już się zakończył", severity="warning")
                return
            self.store.post_message("tui", f"run:{run.run_id}", "cancel", thread=run.run_id)
            self.notify(f"Anulowanie przebiegu {run.run_id}...")
        elif cmd == "/hint":
            tid, _, hint = rest.strip().partition(" ")
            if tid not in run.tasks or not hint.strip():
                self.notify(f"Użycie: /hint <zadanie> <tekst>. Zadania: {', '.join(run.tasks)}", severity="warning")
                return
            self.store.post_message("tui", f"task:{run.run_id}:{tid}", hint.strip(), thread=run.run_id)
            self.notify(f"Wskazówka dla {tid} trafi do następnej próby")
        elif cmd == "/detail":
            tid = rest.strip()
            evs = [e for e in self.store.events(kind="task_attempt", limit=100000)
                   if e.payload.get("run_id") == run.run_id and e.payload.get("task_id") == tid]
            if not evs:
                self.notify(f"Brak prób zadania {tid!r}", severity="warning")
                return
            p = evs[-1].payload
            self._log.write(f"── {tid}, próba {p.get('attempt')}: {p.get('status')} (cwd: {p.get('cwd') or '.'})")
            for label, key in (("test_cmd", "test_cmd"), ("pliki", "files"), ("błąd", "error"),
                               ("wynik testów", "test_output"), ("odpowiedź workera", "worker_text")):
                if p.get(key):
                    self._log.write(f"[{label}] {p[key]}")
        else:
            self.notify("Nieznane polecenie. /help", severity="warning")

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

    # --- nowy agent ---
    def action_new_agent(self) -> None:
        if self.controller is None or self.config is None or self.repo is None:
            self.notify("Nowe sesje wymagają agents.toml i repo (uruchom bez --demo)", severity="warning")
            return
        kinds = [n for n, a in self.config.agents.items() if a.acp]
        self.push_screen(NewAgentScreen(kinds), callback=self._new_agent_chosen)

    def _new_agent_chosen(self, choice: tuple[str, str] | None) -> None:
        if choice is None:
            return
        self.run_worker(self._spawn(*choice), exclusive=False)

    async def _spawn(self, kind: str, name: str) -> None:
        assert self.config is not None and self.repo is not None and self.controller is not None
        from .worktree import create_worktree

        base = name or kind
        existing, n, final = set(self.controller.specs) | set(self.store.agents()), 1, base
        while final in existing:
            n += 1
            final = f"{base}-{n}"
        agent = self.config.agents[kind]
        try:
            wt = await create_worktree(self.repo, name=final)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Nie udało się utworzyć worktree: {exc}", severity="error")
            return
        self.store.add_event(final, "context", {"note": f"nowy agent {kind}"})
        await self.controller.add_agent(AgentSpec(final, agent.acp, str(wt.path), env=agent.acp_env()))
        self.notify(f"Agent {final} uruchomiony w {wt.path}")

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
    ap.add_argument("--agents", default="", help="agenci z agents.toml startowani od razu (po przecinku); resztę dodasz przez Ctrl+N")
    ap.add_argument("--demo", action="store_true", help="dwa agenty mock w katalogu tymczasowym")
    args = ap.parse_args(argv)

    from .node_env import ensure_node

    ensure_node()
    repo = Path(args.repo).resolve()
    store = EventStore(args.db or repo / ".orchestra" / "events.db")

    config = None
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
        config = load_config(args.config)
        specs = asyncio.run(specs_from_config(config, repo, names)) if names else []

    OrchestraApp(store, Controller(store, specs), config=None if args.demo else config,
                 repo=None if args.demo else repo).run()


if __name__ == "__main__":
    main()
