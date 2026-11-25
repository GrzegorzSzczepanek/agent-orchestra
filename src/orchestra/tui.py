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
from textual.widgets import Button, DataTable, Footer, Header, Input, Label, RichLog, Select, Static

from .config import ROLE_NAMES, Config, save_overrides
from .controller import AgentSpec, Controller, orchestra_mcp_server
from .progress import RunView, build_runs
from .render import render_events, status_of
from .store import EventStore
from .worktree import view_diff

HIDDEN_PREFIXES = ("worker:", "role:", "task:")  # wiersze zadań rysujemy z przebiegu, nie z nazw agentów w logu
RUN_KINDS = ("run_start", "task_start", "task_attempt", "task_end", "run_end")
TASK_KINDS = ("task_start", "task_attempt", "task_end", "hint_applied")
ROW_LABEL = {"scheduler": "▶ przebieg zadań", "inbox": "✉ wiadomości"}
TASK_STATUS = {"pending": "○ pending", "running": "● working", "done": "✓ done", "failed": "✗ failed",
               "blocked": "⊘ blocked", "skipped": "– skipped", "over_budget": "$ budżet"}
MAX_RELAY = 10  # tyle wiadomości agent->agent z rzędu przekazujemy dalej, zanim poczekamy na człowieka
# Skróty klawiszowe widać w stopce, więc etykiety są krótkie i mieszczą się też w wąskim panelu (tmux).
BUTTONS = (("b-new", "＋ Agent"), ("b-plan", "▶ Plan"), ("b-msg", "✉ Wiadom."), ("b-hint", "✎ Wskazówka"),
           ("b-detail", "🔎 Info"), ("b-cancel", "⛔ Anuluj"), ("b-diff", "± Diff"), ("b-roles", "⚙ Role"))
STATUS_ICON = {"done": "✓ done", "idle": "○ idle", "working": "● working", "waiting": "? waiting", "error": "✗ error",
               "stopped": "· stopped", "": "·"}


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


class TaskActionScreen(ModalScreen[tuple[str, str] | None]):
    """Wybór zadania z przebiegu i (opcjonalnie) tekstu: wskazówka dla workera albo podgląd szczegółów."""

    BINDINGS = [Binding("escape", "cancel", "Anuluj")]
    DEFAULT_CSS = """
    TaskActionScreen { align: center middle; }
    #act-box { width: 70; height: auto; border: thick $primary; background: $surface; padding: 1 2; }
    #act-box Button { margin-top: 1; width: 100%; }
    """

    def __init__(self, title: str, tasks: list[str], preselect: str = "", with_text: bool = True, button: str = "OK"):
        super().__init__()
        self.title_text, self.tasks, self.preselect, self.with_text, self.button = title, tasks, preselect, with_text, button

    def compose(self) -> ComposeResult:
        with Vertical(id="act-box"):
            yield Label(self.title_text)
            yield Select([(t, t) for t in self.tasks], value=self.preselect if self.preselect in self.tasks else self.tasks[0],
                         allow_blank=False, id="act-task")
            if self.with_text:
                yield Input(placeholder="treść, np. użyj funkcji foo z utils.py", id="act-text")
            yield Button(self.button, id="act-ok", variant="success")
            yield Button("Anuluj", id="act-cancel")

    def _submit(self) -> None:
        text = self.query_one("#act-text", Input).value.strip() if self.with_text else ""
        if self.with_text and not text:
            self.notify("Wpisz treść", severity="warning")
            return
        self.dismiss((str(self.query_one("#act-task", Select).value), text))

    @on(Button.Pressed, "#act-ok")
    def ok(self) -> None:
        self._submit()

    @on(Input.Submitted)
    def submitted(self) -> None:
        self._submit()

    @on(Button.Pressed, "#act-cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class MessageScreen(ModalScreen[tuple[str, str] | None]):
    """Wiadomość od użytkownika do działającego agenta (trafia do niego jako kolejny prompt)."""

    BINDINGS = [Binding("escape", "cancel", "Anuluj")]
    DEFAULT_CSS = """
    MessageScreen { align: center middle; }
    #msg-box { width: 70; height: auto; border: thick $success; background: $surface; padding: 1 2; }
    #msg-box Button { margin-top: 1; width: 100%; }
    """

    def __init__(self, targets: list[str], preselect: str = ""):
        super().__init__()
        self.targets, self.preselect = targets, preselect

    def compose(self) -> ComposeResult:
        with Vertical(id="msg-box"):
            yield Label("Wiadomość do agenta (dostanie ją jako prompt, a odpowiedzieć może przez post_message)")
            yield Select([(t, t) for t in self.targets], value=self.preselect if self.preselect in self.targets else self.targets[0],
                         allow_blank=False, id="msg-to")
            yield Input(placeholder="treść wiadomości", id="msg-text")
            yield Button("Wyślij", id="msg-ok", variant="success")
            yield Button("Anuluj", id="msg-cancel")

    def _submit(self) -> None:
        text = self.query_one("#msg-text", Input).value.strip()
        if not text:
            self.notify("Wpisz treść", severity="warning")
            return
        self.dismiss((str(self.query_one("#msg-to", Select).value), text))

    @on(Button.Pressed, "#msg-ok")
    def ok(self) -> None:
        self._submit()

    @on(Input.Submitted)
    def submitted(self) -> None:
        self._submit()

    @on(Button.Pressed, "#msg-cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class ConfirmScreen(ModalScreen[bool]):
    BINDINGS = [Binding("escape", "no", "Nie")]
    DEFAULT_CSS = """
    ConfirmScreen { align: center middle; }
    #conf-box { width: 64; height: auto; border: thick $error; background: $surface; padding: 1 2; }
    #conf-box Button { margin-top: 1; width: 100%; }
    """

    def __init__(self, question: str):
        super().__init__()
        self.question = question

    def compose(self) -> ComposeResult:
        with Vertical(id="conf-box"):
            yield Label(self.question)
            yield Button("Tak", id="yes", variant="error")
            yield Button("Nie", id="no")

    @on(Button.Pressed)
    def pressed(self, event: Button.Pressed) -> None:
        self.dismiss(event.button.id == "yes")

    def action_no(self) -> None:
        self.dismiss(False)


class RolesScreen(ModalScreen[tuple[dict[str, str], dict[str, str]] | None]):
    """Wybór, który agent pełni role planner, reviewer, implementer, i model wybranego agenta."""

    BINDINGS = [Binding("escape", "cancel", "Anuluj")]
    DEFAULT_CSS = """
    RolesScreen { align: center middle; }
    #roles-box { width: 74; height: auto; max-height: 95%; border: thick $primary; background: $surface; padding: 1 2; }
    #roles-box Button { margin-top: 1; width: 100%; }
    """
    LABEL = {"planner": "planner (układa plan)", "reviewer": "reviewer (ocenia diff)", "implementer": "implementer (worker)"}

    def __init__(self, config: Config):
        super().__init__()
        self.cfg = config

    def _options(self) -> list[tuple[str, str]]:
        return [(f"{n}{'' if a.is_available() else '  (brak w PATH)'}", n) for n, a in self.cfg.agents.items()]

    def compose(self) -> ComposeResult:
        with Vertical(id="roles-box"):
            yield Label("Kto pełni role (brak agenta w PATH: zastąpi go inny)")
            for role in ROLE_NAMES:
                yield Label(self.LABEL[role])
                yield Select(self._options(), value=self.cfg.role_name(role), allow_blank=False, id=f"role-{role}")
            yield Label("Model wybranego agenta (pusto: domyślny)")
            first = self.cfg.role_name("implementer")
            yield Select(self._options(), value=first if first in self.cfg.agents else next(iter(self.cfg.agents)),
                         allow_blank=False, id="model-agent")
            yield Input(value=self.cfg.agents[first].resolve_model() if first in self.cfg.agents else "",
                        placeholder="model, np. deepseek/deepseek-v4.1-flash", id="model-value")
            yield Button("Zapisz", id="roles-ok", variant="success")
            yield Button("Anuluj", id="roles-cancel")

    @on(Select.Changed, "#model-agent")
    def model_agent_changed(self, event: Select.Changed) -> None:
        if event.value in self.cfg.agents:
            self.query_one("#model-value", Input).value = self.cfg.agents[str(event.value)].resolve_model()

    @on(Button.Pressed, "#roles-ok")
    def ok(self) -> None:
        roles = {r: str(self.query_one(f"#role-{r}", Select).value) for r in ROLE_NAMES}
        agent = str(self.query_one("#model-agent", Select).value)
        model = self.query_one("#model-value", Input).value.strip()
        current = self.cfg.agents[agent].resolve_model()
        self.dismiss((roles, {agent: model} if model != current else {}))

    @on(Button.Pressed, "#roles-cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class PlanScreen(ModalScreen[tuple[str, str] | None]):
    """Własny podział pracy: cel dla plannera albo gotowy plik planu JSON (bez klienta typu Claude Code)."""

    BINDINGS = [Binding("escape", "cancel", "Anuluj")]
    DEFAULT_CSS = """
    PlanScreen { align: center middle; }
    #plan-box { width: 76; height: auto; border: thick $success; background: $surface; padding: 1 2; }
    #plan-box Button { margin-top: 1; width: 100%; }
    """

    def __init__(self, planner: str, default_file: str):
        super().__init__()
        self.planner, self.default_file = planner, default_file

    def compose(self) -> ComposeResult:
        with Vertical(id="plan-box"):
            yield Label(f"Cel: planner ({self.planner}) rozbije go na zadania")
            yield Input(placeholder="np. dodaj walidację e-maili wraz z testami", id="plan-goal")
            yield Button("Zaplanuj i uruchom", id="plan-goal-ok", variant="success")
            yield Label("Albo własny podział: plik planu JSON (szablon: init-plan)")
            yield Input(value=self.default_file, placeholder="ścieżka do plan.json", id="plan-file")
            yield Button("Uruchom plan z pliku", id="plan-file-ok", variant="primary")
            yield Button("Anuluj", id="plan-cancel")

    @on(Button.Pressed, "#plan-goal-ok")
    def goal_ok(self) -> None:
        goal = self.query_one("#plan-goal", Input).value.strip()
        if goal:
            self.dismiss(("goal", goal))
        else:
            self.notify("Wpisz cel", severity="warning")

    @on(Button.Pressed, "#plan-file-ok")
    def file_ok(self) -> None:
        path = self.query_one("#plan-file", Input).value.strip()
        if path:
            self.dismiss(("file", path))
        else:
            self.notify("Podaj ścieżkę pliku", severity="warning")

    @on(Input.Submitted, "#plan-goal")
    def goal_submitted(self) -> None:
        self.goal_ok()

    @on(Input.Submitted, "#plan-file")
    def file_submitted(self) -> None:
        self.file_ok()

    @on(Button.Pressed, "#plan-cancel")
    def cancel_button(self) -> None:
        self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


class OrchestraApp(App[None]):
    TITLE = "agent-orchestra"
    CSS = """
    #toolbar { height: 3; }
    #toolbar Button { margin-right: 0; min-width: 0; padding: 0 1; }
    #rolebar { height: 1; color: $text-muted; padding: 0 1; }
    #side { width: 40%; min-width: 30; max-width: 46; border-right: solid $primary; }
    #main { width: 1fr; }
    #log, #diff { height: 1fr; }
    #diff { display: none; border-top: solid $secondary; }
    #hint { height: auto; color: $text-muted; padding: 0 1; }
    #prompt { dock: bottom; }
    """
    BINDINGS = [
        Binding("ctrl+n", "new_agent", "Nowy agent"),
        Binding("f3", "message", "Wiadomość"),
        Binding("f4", "hint", "Wskazówka"),
        Binding("f5", "detail", "Szczegóły"),
        Binding("f6", "cancel_run", "Anuluj przebieg"),
        Binding("f7", "plan", "Plan"),
        Binding("f8", "roles", "Role"),
        Binding("f2", "toggle_diff", "Diff"),
        Binding("f9", "toggle_full_log", "Pełny log"),
        Binding("ctrl+q", "quit", "Wyjście"),
    ]

    def __init__(self, store: EventStore, controller: Controller | None = None, poll_s: float = 0.2,
                 config: Config | None = None, repo: Path | None = None, config_path: Path | None = None):
        super().__init__()
        self.store, self.controller, self.poll_s = store, controller, poll_s
        self.config, self.repo, self.config_path = config, repo, config_path
        self.selected = ""
        self.compact = True  # F9 przełącza na pełny log (rozumowanie, surowe wyniki narzędzi, pełne ścieżki)
        self._last_id = 0
        self._status: dict[str, str] = {}
        self._run_events: list = []
        self._task_run = ""
        self._msg_last = 0
        self._relay_chain = 0
        if controller is not None:
            controller.permission_handler = self.ask_permission

    # --- układ ---
    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="toolbar"):
            for bid, label in BUTTONS:
                yield Button(label, id=bid)
        yield Static("", id="rolebar", markup=False)
        with Horizontal():
            with Vertical(id="side"):
                yield DataTable(id="agents", cursor_type="row")
            with Vertical(id="main"):
                yield RichLog(id="log", wrap=True, min_width=20, highlight=False, markup=False)
                yield RichLog(id="diff", wrap=False, highlight=False, markup=False)
                yield Static("", id="hint")
                yield Input(placeholder="Zaznacz agenta albo zadanie po lewej", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        # referencje do widgetów: self.query_one szuka na aktywnym ekranie, a nie gdy na wierzchu jest modal
        self._table = self.query_one("#agents", DataTable)
        self._log = self.query_one("#log", RichLog)
        self._diff = self.query_one("#diff", RichLog)
        self._hint = self.query_one("#hint", Static)
        self._prompt = self.query_one("#prompt", Input)
        self._rolebar = self.query_one("#rolebar", Static)
        self._jobs: set[asyncio.Task] = set()
        # Tab ma iść z tabeli prosto do promptu (logi przewijasz myszą, przyciski klikasz albo F-klawisze)
        self._log.can_focus = False
        self._diff.can_focus = False
        for b in self.query("#toolbar Button"):
            b.can_focus = False
        table = self._table
        for key, label in (("agent", "agent / zadanie"), ("status", "status"), ("tin", "in"), ("tout", "out"), ("cost", "$")):
            table.add_column(label, key=key)
        msgs = self.store.messages(0, limit=1_000_000)
        self._msg_last = msgs[-1].id if msgs else 0  # stare wiadomości nie są odtwarzane agentom
        self.set_interval(self.poll_s, self.poll)
        self.set_interval(2.0, self.refresh_roles)  # wybór ról mógł zmienić `orchestra roles` albo inny proces
        self.refresh_roles()
        if self.controller is not None:
            self.run_worker(self.controller.start_all(), exclusive=False)
        self.poll()

    def refresh_roles(self) -> None:
        if self.config is None or self.repo is None:
            self._rolebar.update("")
            return
        self.config.reload_overrides(self.repo)
        parts = []
        for role in ROLE_NAMES:
            info = self.config.role_info(role)
            model = self.config.agents[info.effective].resolve_model() if info.effective in self.config.agents else ""
            parts.append(f"{role}: {info.effective or '?'}{f' [{model}]' if model and role == 'implementer' else ''}{' *' if info.changed else ''}")
        self._rolebar.update("Role: " + " | ".join(parts) + "   (* = zmiana, F8)")

    async def on_unmount(self) -> None:
        for job in self._jobs:
            job.cancel()
        if self.controller is not None:
            await self.controller.aclose()

    # --- który wiersz widzi które zdarzenia ---
    def _belongs(self, ev, sel: str) -> bool:
        if sel.startswith("task:"):
            tid = sel[5:]
            return ev.agent == sel or (ev.kind in TASK_KINDS and ev.payload.get("task_id") == tid
                                       and ev.payload.get("run_id") == self._task_run)
        return ev.agent == sel

    # --- polling logu zdarzeń ---
    def poll(self) -> None:
        events = self.store.events(since_id=self._last_id, limit=2000)
        table = self._table
        run_changed = False
        if events:
            self._last_id = events[-1].id
            for ev in events:
                if ev.kind in RUN_KINDS:
                    self._run_events.append(ev)
                    run_changed = True
                if ev.agent.startswith(HIDDEN_PREFIXES):
                    continue
                if ev.agent not in self._status:
                    self._status[ev.agent] = ""
                    table.add_row(ROW_LABEL.get(ev.agent, ev.agent), "", "0", "0", "0.00", key=ev.agent)
                self._status[ev.agent] = status_of(ev.kind, self._status[ev.agent])
            if run_changed:
                self._refresh_tasks()
            if not self.selected and self._status:
                self.select_agent(next(iter(self._status)))
            elif self.selected:
                for line in render_events([e for e in events if self._belongs(e, self.selected)], self.compact):
                    self._log.write(line)
        for agent, tot in self.store.totals().items():
            if agent in self._status and not agent.startswith("task:"):
                table.update_cell(agent, "status", STATUS_ICON.get(self._status[agent], self._status[agent]), update_width=True)
                table.update_cell(agent, "tin", f"{int(tot['tokens_in'])}", update_width=True)
                table.update_cell(agent, "tout", f"{int(tot['tokens_out'])}", update_width=True)
                table.update_cell(agent, "cost", f"{tot['cost']:.2f}", update_width=True)
        self._relay()

    def _latest_run(self) -> RunView | None:
        runs = list(build_runs(self._run_events).values())
        active = [r for r in runs if not r.finished]
        return (active or runs or [None])[-1]

    def _refresh_tasks(self) -> None:
        """Wiersze zadań najnowszego przebiegu: stan, tokeny, widoczne od razu po run_plan."""
        run, table = self._latest_run(), self._table
        if run is None:
            return
        if run.run_id != self._task_run:  # nowy przebieg: stare wiersze zadań znikają
            for key in [k for k in self._status if k.startswith("task:")]:
                table.remove_row(key)
                del self._status[key]
            self._task_run = run.run_id
        for t in run.tasks.values():
            key, status = f"task:{t.id}", TASK_STATUS.get(t.status, t.status)
            if key not in self._status:
                self._status[key] = t.status
                table.add_row(f"   ▸ {t.id}", status, str(t.tokens_in), str(t.tokens_out), "-", key=key)
            else:
                table.update_cell(key, "status", status + (f" ({t.attempts})" if t.attempts else ""), update_width=True)
                table.update_cell(key, "tin", str(t.tokens_in), update_width=True)
                table.update_cell(key, "tout", str(t.tokens_out), update_width=True)
        done = sum(t.status == "done" for t in run.tasks.values())
        self.sub_title = f"przebieg {run.run_id}: {done}/{len(run.tasks)} gotowe" + (" (zakończony)" if run.finished else "")

    def select_agent(self, name: str) -> None:
        self.selected = name
        log = self._log
        log.clear()
        if name.startswith("task:"):
            evs = [e for e in self.store.events(limit=100000) if self._belongs(e, name)]
        else:
            evs = self.store.events(agent=name, limit=5000)
        for line in render_events(evs, self.compact):
            log.write(line)
        self._update_hint()

    def action_toggle_full_log(self) -> None:
        self.compact = not self.compact
        if self.selected:
            self.select_agent(self.selected)
        self.notify("Log zwięzły" if self.compact else "Log pełny (F9 wraca do zwięzłego)")

    def _update_hint(self) -> None:
        name = self.selected
        if name.startswith("task:"):
            tid = name[5:]
            self._hint.update(f"Zadanie {tid}. Tekst poniżej to wskazówka dla następnej próby. "
                              "F5 błąd i testy, F6 anuluj, F9 pełny log.")
            self._prompt.placeholder = f"Wskazówka dla zadania {tid} + Enter"
        elif self.controller is not None and name in self.controller.specs:
            self._hint.update(f"Agent {name}: tekst poniżej to prompt. F3 wysyła wiadomość od Ciebie do dowolnego agenta, "
                              "agenci piszą do siebie sami (post_message), widać to w wierszu wiadomości.")
            self._prompt.placeholder = f"Prompt do {name} + Enter"
        elif name == "scheduler":
            self._hint.update("Przebieg zadań (run_plan): start, próby, wyniki. Zaznacz zadanie pod spodem, żeby zobaczyć workera.")
            self._prompt.placeholder = "Zaznacz agenta (prompt) albo zadanie (wskazówka)"
        elif name == "inbox":
            self._hint.update("Wszystkie wiadomości: użytkownik, agenci i recenzent.")
            self._prompt.placeholder = "Zaznacz agenta (prompt) albo zadanie (wskazówka)"
        else:
            self._hint.update("")
            self._prompt.placeholder = "Zaznacz agenta (prompt) albo zadanie (wskazówka)"

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
        if not text:
            return
        if not self.selected:  # prompt przed pierwszym poll: nie gub tekstu, wybierz pierwszego agenta
            agents = [a for a in self.store.agents() if not a.startswith(HIDDEN_PREFIXES)]
            if not agents:
                return
            self.select_agent(agents[0])
        if self.selected.startswith("task:"):
            self.send_hint(self.selected[5:], text)
        elif self.controller is not None and self.selected in self.controller.specs:
            self.run_worker(self.controller.send(self.selected, text), exclusive=False)
        else:
            self.notify("Zaznacz agenta (prompt) albo zadanie (wskazówka) po lewej", severity="warning")

    # --- akcje z przycisków i F-klawiszy ---
    @on(Button.Pressed, "#toolbar Button")
    def toolbar_pressed(self, event: Button.Pressed) -> None:
        {"b-new": self.action_new_agent, "b-plan": self.action_plan, "b-roles": self.action_roles,
         "b-msg": self.action_message, "b-hint": self.action_hint,
         "b-detail": self.action_detail, "b-cancel": self.action_cancel_run,
         "b-diff": lambda: self.run_worker(self.action_toggle_diff(), exclusive=False)}[event.button.id or ""]()

    def _tasks_or_warn(self) -> RunView | None:
        run = self._latest_run()
        if run is None or not run.tasks:
            self.notify("Brak przebiegu run_plan. Poproś Claude'a o rozbicie zadania na workery.", severity="warning")
            return None
        return run

    def action_message(self) -> None:
        targets = list(self.controller.sessions) if self.controller else []
        if not targets:
            self.notify("Brak działających agentów. Dodaj jednego przyciskiem ＋ Agent (Ctrl+N)", severity="warning")
            return
        self.push_screen(MessageScreen(targets, self.selected), callback=self._message_chosen)

    def _message_chosen(self, choice: tuple[str, str] | None) -> None:
        if choice:
            self.store.post_message("user", choice[0], choice[1])  # dostarczy _relay przy najbliższym poll
            self.notify(f"Wysłano do {choice[0]}")

    def action_hint(self) -> None:
        run = self._tasks_or_warn()
        if run is not None:
            pre = self.selected[5:] if self.selected.startswith("task:") else ""
            self.push_screen(TaskActionScreen("Wskazówka dla workera (dołączana do następnej próby)", list(run.tasks), pre,
                                              True, "Wyślij wskazówkę"), callback=self._hint_chosen)

    def _hint_chosen(self, choice: tuple[str, str] | None) -> None:
        if choice:
            self.send_hint(*choice)

    def send_hint(self, tid: str, text: str) -> None:
        run = self._latest_run()
        if run is None or tid not in run.tasks:
            self.notify("Brak takiego zadania w przebiegu", severity="warning")
            return
        if run.finished:
            self.notify("Przebieg już się zakończył, wskazówka nie zadziała. Poproś Claude'a o ponowienie zadania.",
                        severity="warning")
            return
        self.store.post_message("user", f"task:{run.run_id}:{tid}", text, thread=run.run_id)
        self.notify(f"Wskazówka dla {tid} trafi do następnej próby workera")

    def action_detail(self) -> None:
        run = self._tasks_or_warn()
        if run is None:
            return
        if self.selected.startswith("task:"):
            self.show_detail(self.selected[5:])
        else:
            self.push_screen(TaskActionScreen("Szczegóły zadania", list(run.tasks), "", False, "Pokaż"),
                             callback=lambda c: c and self.show_detail(c[0]))

    def action_cancel_run(self) -> None:
        run = self._latest_run()
        if run is None or run.finished:
            self.notify("Brak trwającego przebiegu", severity="warning")
            return
        self.push_screen(ConfirmScreen(f"Anulować przebieg {run.run_id}? Pracujące workery zostaną zabite."),
                         callback=lambda yes: yes and self._do_cancel(run.run_id))

    def _do_cancel(self, run_id: str) -> None:
        self.store.post_message("user", f"run:{run_id}", "cancel", thread=run_id)
        self.notify(f"Anulowanie przebiegu {run_id}...")

    def show_detail(self, tid: str) -> None:
        run = self._latest_run()
        evs = [e for e in self.store.events(kind="task_attempt", limit=100000)
               if run is not None and e.payload.get("run_id") == run.run_id and e.payload.get("task_id") == tid]
        key = f"task:{tid}"
        if key in self._status:
            self.select_agent(key)
            try:
                self._table.move_cursor(row=self._table.get_row_index(key))
            except Exception:  # noqa: BLE001
                pass
        if not evs:
            self._log.write(f"── {tid}: brak zakończonych prób (worker jeszcze pracuje albo czeka na zależności)")
            return
        p = evs[-1].payload
        self._log.write(f"── {tid}, próba {p.get('attempt')}: {p.get('status')} (katalog: {p.get('cwd') or '.'})")
        for label, k in (("test_cmd", "test_cmd"), ("pliki", "files"), ("błąd", "error"),
                         ("wynik testów", "test_output"), ("odpowiedź workera", "worker_text")):
            if p.get(k):
                self._log.write(f"[{label}] {p[k]}")

    # --- skróty tekstowe (opcjonalne, to samo co przyciski) ---
    def run_command(self, text: str) -> None:
        cmd, _, rest = text.partition(" ")
        if cmd == "/cancel":
            self.action_cancel_run()
        elif cmd == "/hint":
            tid, _, hint = rest.strip().partition(" ")
            if not hint.strip():
                self.notify("Użycie: /hint <zadanie> <tekst> (albo przycisk Wskazówka)", severity="warning")
            else:
                self.send_hint(tid, hint.strip())
        elif cmd == "/detail":
            self.show_detail(rest.strip())
        elif cmd == "/msg":
            to, _, body = rest.strip().partition(" ")
            if to and body.strip():
                self._message_chosen((to, body.strip()))
            else:
                self.notify("Użycie: /msg <agent> <tekst> (albo przycisk Wiadomość)", severity="warning")
        else:
            self.notify("Skróty: /hint, /detail, /cancel, /msg. Wszystko to samo robią przyciski u góry.", severity="warning")

    # --- komunikacja agent <-> agent ---
    def _relay(self) -> None:
        """Wiadomości (od Ciebie albo od agentów przez post_message) do działającego agenta trafiają do niego jako prompt."""
        if self.controller is None:
            return
        for m in self.store.messages(self._msg_last):
            self._msg_last = m.id
            if m.to_agent not in self.controller.sessions or m.to_agent == m.from_agent:
                continue
            if m.from_agent == "user":
                self._relay_chain = 0
            else:
                self._relay_chain += 1
                if self._relay_chain > MAX_RELAY:
                    if self._relay_chain == MAX_RELAY + 1:
                        self.notify(f"Agenci wymienili {MAX_RELAY} wiadomości bez Ciebie, wstrzymuję przekazywanie. "
                                    "Napisz do któregoś (F3), żeby wznowić.", severity="warning")
                    continue
            text = (f"[orchestra] Wiadomość od {m.from_agent}: {m.body}\n"
                    f"(Jeśli trzeba odpowiedzieć, użyj narzędzia post_message do {m.from_agent}. "
                    "Nie odpowiadaj na samo potwierdzenie.)")
            self.run_worker(self.controller.send(m.to_agent, text), exclusive=False)

    # --- role ---
    def action_roles(self) -> None:
        if self.config is None or self.repo is None:
            self.notify("Role wymagają agents.toml i repo (uruchom bez --demo)", severity="warning")
            return
        self.push_screen(RolesScreen(self.config), callback=self._roles_chosen)

    def _roles_chosen(self, choice: tuple[dict[str, str], dict[str, str]] | None) -> None:
        if choice is None or self.config is None or self.repo is None:
            return
        roles, models = choice
        save_overrides(self.repo, roles=roles, models=models)
        self.refresh_roles()
        self.notify("Zapisano role. Obowiązują od następnego przebiegu (także dla Claude'a przez MCP).")

    # --- plan: cel dla plannera albo własny plik ---
    def action_plan(self) -> None:
        if self.config is None or self.repo is None:
            self.notify("Plan wymaga agents.toml i repo (uruchom bez --demo)", severity="warning")
            return
        default = next((p for p in (".orchestra/plan.json", "plan.json") if (self.repo / p).is_file()), "")
        self.push_screen(PlanScreen(self.config.role_name("planner"), default), callback=self._plan_chosen)

    def _plan_chosen(self, choice: tuple[str, str] | None) -> None:
        if choice is None:
            return
        job = asyncio.ensure_future(self._plan_job(*choice))
        self._jobs.add(job)
        job.add_done_callback(self._jobs.discard)

    async def _plan_job(self, mode: str, text: str) -> None:
        assert self.config is not None and self.repo is not None
        from .agents_io import headless_ask, repo_files
        from .plan import Plan, PlanError, make_plan
        from .scheduler import RunOptions, Scheduler

        self.config.reload_overrides(self.repo)
        try:
            if mode == "file":
                path = Path(text).expanduser()
                path = path if path.is_absolute() else self.repo / path
                plan = Plan.model_validate_json(path.read_text(encoding="utf-8"))
            else:
                planner = self.config.role_name("planner")
                self.notify(f"Planuję przez {planner}, to może potrwać do minuty...")
                ask = headless_ask(self.config, "planner", self.repo, store=self.store)
                plan = await make_plan(text, ask, repo_files=await repo_files(self.repo))
        except (OSError, ValueError, PlanError, RuntimeError, KeyError) as exc:
            self.notify(f"Plan nieudany: {exc}", severity="error", timeout=15)
            return
        self.notify(f"Plan: {len(plan.tasks)} zadań, startuję workery")
        opts = RunOptions(parallel=2, escalate=False)  # porażka zostaje widoczna, bez cichej eskalacji do drogiego modelu
        try:
            report = await Scheduler(self.config, self.repo, self.store, opts).run(plan)
        except Exception as exc:  # noqa: BLE001
            self.notify(f"Przebieg przerwany: {exc}", severity="error", timeout=15)
            return
        self.notify(f"Przebieg {report.run_id} zakończony. Gałąź do przeglądu: {report.integration_branch}", timeout=20)

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
        mcp = (orchestra_mcp_server(final, self.repo, self.config_path, self.store.path),) if self.config_path else ()
        await self.controller.add_agent(AgentSpec(final, agent.acp, str(wt.path), mcp_servers=mcp, env=agent.acp_env()))
        self.notify(f"Agent {final} uruchomiony w {wt.path}")

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
    ap.add_argument("--agents", default="", help="agenci z agents.toml startowani od razu (po przecinku); resztę dodasz przyciskiem ＋ Agent")
    ap.add_argument("--demo", action="store_true", help="dwa agenty mock w katalogu tymczasowym")
    args = ap.parse_args(argv)

    from .node_env import ensure_node

    ensure_node()
    repo = Path(args.repo).resolve()
    store = EventStore(args.db or repo / ".orchestra" / "events.db")

    config, config_path = None, None
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

        from .worktree import ensure_local_exclude

        ensure_local_exclude(repo)
        config_path = Path(args.config).resolve()
        config = load_config(config_path, repo=repo)
        names = [n for n in args.agents.split(",") if n]
        if not names:  # domyślnie jedna sesja agenta roli implementer, o ile jest zainstalowany i mówi ACP
            impl = config.role_name("implementer")
            if impl in config.agents and config.agents[impl].acp and config.available(impl):
                names = [impl]
        specs = asyncio.run(specs_from_config(config, repo, names, config_path=config_path, db_path=store.path)) if names else []

    OrchestraApp(store, Controller(store, specs), config=config, repo=None if args.demo else repo,
                 config_path=config_path).run()


if __name__ == "__main__":
    main()
