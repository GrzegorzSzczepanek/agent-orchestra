"""`orchestra watch`: podgląd na żywo przebiegów run_plan i delegowań z logu zdarzeń (tylko odczyt)."""

from __future__ import annotations

import re
import time
from pathlib import Path

from rich.text import Text
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import DataTable, Footer, Header, RichLog, Static

from .progress import RunView, build_runs
from .store import Event, EventStore

STATUS_STYLE = {"pending": "dim", "running": "bold yellow", "done": "bold green", "failed": "bold red",
                "blocked": "red", "skipped": "dim red", "over_budget": "red"}
STATUS_ICON = {"pending": "○", "running": "●", "done": "✓", "failed": "✗", "blocked": "⊘", "skipped": "–",
               "over_budget": "$"}


# --- widok zwięzły: to, co człowiek chce śledzić (co worker robi, co mówi, czy testy przechodzą) ---
_WORKTREE = re.compile(r"(?:/[^\s:'\"]+)?/\.orchestra/worktrees/[^/\s:'\"]+/?")
_VENV = re.compile(r"(?:/[^\s:'\"]+)?/\.venv/bin/")
_OUTCOME = re.compile(r"\b\d+ (?:passed|failed)\b[^\[\n]*|\bTraceback\b.*|\bno tests ran\b.*")
TOOL_LABEL = {"read_file": "czyta", "read_multiple_files": "czyta", "read_directory": "przegląda", "grep": "szuka",
              "glob": "szuka plików", "write_file": "zapisuje", "edit_file": "edytuje", "shell_command": "$"}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tidy(text: str) -> str:
    """Skraca ścieżki worktree i venv, które zajmują pół ekranu i nic nie mówią."""
    return _VENV.sub("", _WORKTREE.sub("", text))


def _compact_step(step: str, text: str) -> Text | None:
    text = " ".join(_tidy(text).split())
    if step == "tool":
        name, _, detail = text.partition(": ")
        return Text.assemble(("  " + TOOL_LABEL.get(name, name) + " ", "yellow"), (_clip(detail or ".", 90), ""))
    if step == "result":  # pokazujemy tylko wynik testów i błędy, reszta to zrzuty plików
        found = _OUTCOME.search(text)
        if not found:
            return None
        summary = found.group(0).strip()
        bad = "failed" in summary or "Traceback" in summary or "no tests" in summary
        return Text("    ↳ " + _clip(summary, 90), style="bold red" if bad else "bold green")
    if step == "text":
        return Text("\n" + _clip(text, 240), style="green")
    return None  # rozumowanie modelu


def fmt_event(ev: Event, compact: bool = False) -> Text | None:
    """Jedna zwięzła linia dla zdarzeń orkiestracji. Strumień wiadomości agentów pomijamy (to robi orchestra-tui)."""
    p, clock = ev.payload, time.strftime("%H:%M:%S", time.localtime(ev.ts))
    tok = f" {ev.tokens_in}/{ev.tokens_out} tok" if ev.tokens_in or ev.tokens_out else ""
    match ev.kind:
        case "run_start":
            return Text(f"{clock} ▶ run {p.get('run_id')}: {p.get('tasks')} zadań, równolegle {p.get('parallel')}",
                        style="bold cyan")
        case "task_start":
            return Text(f"{clock}   ● {p.get('task_id')} start ({p.get('difficulty')})", style="yellow")
        case "task_attempt" if compact:
            ok = p.get("status") == "ok"
            esc = ", eskalacja" if p.get("escalated") else ""
            total = ev.tokens_in + ev.tokens_out
            used = f", {round(total / 1000)}k tok" if total >= 1000 else ""
            text = Text(f"\n{'✓' if ok else '✗'} próba {p.get('attempt')}: {p.get('status')}{esc}{used}",
                        style="bold green" if ok else "bold red")
            if not ok:
                why = (p.get("error", "")[-300:] + "\n" + p.get("test_output", "")).strip()
                tail = [_clip(_tidy(ln), 110) for ln in why.splitlines() if ln.strip()][-4:]
                if tail:
                    text.append("\n" + "\n".join("  " + ln for ln in tail), style="red")
                    text.append("\n  F5: pełny wynik", style="dim")
            return text
        case "task_attempt":
            ok = p.get("status") == "ok"
            esc = " (eskalacja)" if p.get("escalated") else ""
            text = Text(f"{clock}   {ev.agent} {p.get('task_id')} próba {p.get('attempt')}: {p.get('status')}{esc}{tok}",
                        style="green" if ok else "red")
            if not ok:
                why = "\n".join(filter(None, [p.get("error", "")[-300:], "\n".join(p.get("test_output", "").splitlines()[-6:])]))
                if why:
                    text.append("\n" + "\n".join("      " + ln for ln in why.splitlines()[-8:]), style="dim red")
                    text.append(f"\n      (pełny wynik: /detail {p.get('task_id')})", style="dim")
            return text
        case "worker_step" if compact:
            return _compact_step(p.get("step", ""), p.get("text", ""))
        case "worker_end" if compact:
            return None  # to samo mówi task_attempt
        case "worker_step":
            style = {"tool": "yellow", "result": "dim", "text": "green", "think": "dim italic"}.get(p.get("step", ""), "")
            mark = {"tool": "⚙", "result": "  ↳", "text": "💬", "think": "…"}.get(p.get("step", ""), "·")
            return Text(f"{clock} {mark} {p.get('text', '')}", style=style)
        case "worker_end":
            ok = p.get("status") == "ok"
            return Text(f"{clock} ■ próba {p.get('attempt')}: {p.get('status')}", style="green" if ok else "red")
        case "message":
            return Text(f"{clock} ✉ {p.get('from')} → {p.get('to')}: {p.get('body', '')[:400]}", style="bold blue")
        case "hint_applied":
            return Text(f"{clock} ✎ wskazówka dla {p.get('task_id')}: {p.get('text', '')}", style="bold magenta")
        case "run_cancel":
            return Text(f"{clock} ✗ przebieg anulowany ({p.get('by', '')})", style="bold red")
        case "task_end":
            style = STATUS_STYLE.get(p.get("status", ""), "")
            return Text(f"{clock}   {STATUS_ICON.get(p.get('status', ''), '?')} {p.get('task_id')} {p.get('status')}",
                        style=style)
        case "run_end":
            return Text(f"{clock} ■ run {p.get('run_id')} {'OK' if p.get('ok') else 'z błędami'}, "
                        f"gałąź {p.get('integration_branch')}", style="bold cyan" if p.get("ok") else "bold red")
        case "delegate":
            return Text(f"{clock} ⇒ delegate {ev.agent}: {p.get('status')} ({p.get('branch', '')}){tok}", style="magenta")
        case "role_attempt":
            return Text(f"{clock} ⚑ rola {ev.agent}: {p.get('status', '')}{tok}", style="blue")
        case "error":
            return Text(f"{clock} ✗ {ev.agent}: {p.get('error', '')}", style="bold red")
    return None


def _elapsed(run: RunView) -> str:
    end = run.ended or time.time()
    s = int(max(0, end - run.started))
    return f"{s // 60}m{s % 60:02d}s"


class WatchApp(App[None]):
    TITLE = "orchestra watch"
    CSS = """
    #head { height: 3; padding: 0 1; border-bottom: solid $primary; }
    #tasks { height: 1fr; }
    #feed { height: 1fr; border-top: solid $secondary; }
    """
    BINDINGS = [Binding("q", "quit", "Wyjście"), Binding("n", "next_run", "Następny run")]

    def __init__(self, store: EventStore, poll_s: float = 0.5):
        super().__init__()
        self.store, self.poll_s = store, poll_s
        self._last_id = 0
        self._events: list[Event] = []
        self._run_idx = -1  # -1: zawsze najnowszy run

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Czekam na zdarzenia...", id="head")
        yield DataTable(id="tasks", cursor_type="row")
        yield RichLog(id="feed", wrap=True, markup=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self._head, self._table, self._feed = (self.query_one("#head", Static), self.query_one("#tasks", DataTable),
                                              self.query_one("#feed", RichLog))
        self._feed.can_focus = False
        for key, label in (("task", "zadanie"), ("status", "status"), ("diff", "trudność"), ("deps", "po"),
                           ("worker", "worker"), ("att", "próby"), ("tok", "tokeny in/out"), ("desc", "opis")):
            self._table.add_column(label, key=key)
        self.set_interval(self.poll_s, self.poll)
        self.poll()

    def action_next_run(self) -> None:
        n = len(build_runs(self._events))
        if n:
            self._run_idx = (self._run_idx + 1) % n
            self.refresh_table()

    def poll(self) -> None:
        new = self.store.events(since_id=self._last_id, limit=5000)
        if not new:
            return
        self._last_id = new[-1].id
        self._events += [e for e in new if e.kind in ("run_start", "task_start", "task_attempt", "task_end", "run_end")]
        for ev in new:
            line = fmt_event(ev)
            if line is not None:
                self._feed.write(line)
        self.refresh_table()

    def refresh_table(self) -> None:
        runs = list(build_runs(self._events).values())
        if not runs:
            return
        run = runs[self._run_idx]
        done = sum(t.status == "done" for t in run.tasks.values())
        state = ("OK" if run.ok else "zakończony z błędami") if run.finished else "w toku"
        self._head.update(Text.assemble(
            (f"run {run.run_id}", "bold"), f"  {state}  {_elapsed(run)}  {done}/{len(run.tasks)} gotowe  "
            f"gałąź {run.integration_branch}\n", (run.goal[:200], "italic")))
        self._table.clear()
        for t in run.tasks.values():
            style = STATUS_STYLE.get(t.status, "")
            attempt = f"{t.attempts}" + (" ↑" if t.escalated else "") + (f" ({t.last_attempt})" if t.last_attempt else "")
            self._table.add_row(t.id, Text(f"{STATUS_ICON.get(t.status, '?')} {t.status}", style=style), t.difficulty,
                                ",".join(t.depends_on) or "-", t.worker or "-", attempt,
                                f"{t.tokens_in}/{t.tokens_out}", t.description[:60])


def main(repo: str | Path = ".", db: str | None = None) -> None:
    store = EventStore(db or Path(repo).resolve() / ".orchestra" / "events.db")
    WatchApp(store).run()
