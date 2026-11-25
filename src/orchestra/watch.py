"""`orchestra watch`: live view of run_plan runs and delegations from the event log (read only)."""

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


# --- compact view: what a human wants to follow (what the worker does and says, whether tests pass) ---
_WORKTREE = re.compile(r"(?:/[^\s:'\"]+)?/\.orchestra/worktrees/[^/\s:'\"]+/?")
_VENV = re.compile(r"(?:/[^\s:'\"]+)?/\.venv/bin/")
_OUTCOME = re.compile(r"\b\d+ (?:passed|failed)\b[^\[\n]*|\bTraceback\b.*|\bno tests ran\b.*")
TOOL_LABEL = {"read_file": "reads", "read_multiple_files": "reads", "read_directory": "browses", "grep": "searches",
              "glob": "finds files", "write_file": "writes", "edit_file": "edits", "shell_command": "$"}


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _tidy(text: str) -> str:
    """Shortens worktree and venv paths, which take half the screen and say nothing."""
    return _VENV.sub("", _WORKTREE.sub("", text))


def _compact_step(step: str, text: str) -> Text | None:
    text = " ".join(_tidy(text).split())
    if step == "tool":
        name, _, detail = text.partition(": ")
        return Text.assemble(("  " + TOOL_LABEL.get(name, name) + " ", "yellow"), (_clip(detail or ".", 90), ""))
    if step == "result":  # show only test outcomes and errors, the rest is file dumps
        found = _OUTCOME.search(text)
        if not found:
            return None
        summary = found.group(0).strip()
        bad = "failed" in summary or "Traceback" in summary or "no tests" in summary
        return Text("    ↳ " + _clip(summary, 90), style="bold red" if bad else "bold green")
    if step == "text":
        return Text("\n" + _clip(text, 240), style="green")
    return None  # model reasoning


def fmt_event(ev: Event, compact: bool = False) -> Text | None:
    """One compact line for orchestration events. The agent message stream is skipped (orchestra-tui shows it)."""
    p, clock = ev.payload, time.strftime("%H:%M:%S", time.localtime(ev.ts))
    tok = f" {ev.tokens_in}/{ev.tokens_out} tok" if ev.tokens_in or ev.tokens_out else ""
    match ev.kind:
        case "run_start":
            return Text(f"{clock} ▶ run {p.get('run_id')}: {p.get('tasks')} tasks, parallel {p.get('parallel')}",
                        style="bold cyan")
        case "task_start":
            return Text(f"{clock}   ● {p.get('task_id')} start ({p.get('difficulty')})", style="yellow")
        case "task_attempt" if compact:
            ok = p.get("status") == "ok"
            esc = ", escalation" if p.get("escalated") else ""
            total = ev.tokens_in + ev.tokens_out
            used = f", {round(total / 1000)}k tok" if total >= 1000 else ""
            text = Text(f"\n{'✓' if ok else '✗'} attempt {p.get('attempt')}: {p.get('status')}{esc}{used}",
                        style="bold green" if ok else "bold red")
            if not ok:
                why = (p.get("error", "")[-300:] + "\n" + p.get("test_output", "")).strip()
                tail = [_clip(_tidy(ln), 110) for ln in why.splitlines() if ln.strip()][-4:]
                if tail:
                    text.append("\n" + "\n".join("  " + ln for ln in tail), style="red")
                    text.append("\n  F5: full output", style="dim")
            return text
        case "task_attempt":
            ok = p.get("status") == "ok"
            esc = " (escalation)" if p.get("escalated") else ""
            text = Text(f"{clock}   {ev.agent} {p.get('task_id')} attempt {p.get('attempt')}: {p.get('status')}{esc}{tok}",
                        style="green" if ok else "red")
            if not ok:
                why = "\n".join(filter(None, [p.get("error", "")[-300:], "\n".join(p.get("test_output", "").splitlines()[-6:])]))
                if why:
                    text.append("\n" + "\n".join("      " + ln for ln in why.splitlines()[-8:]), style="dim red")
                    text.append(f"\n      (full output: /detail {p.get('task_id')})", style="dim")
            return text
        case "worker_step" if compact:
            return _compact_step(p.get("step", ""), p.get("text", ""))
        case "worker_end" if compact:
            return None  # task_attempt says the same
        case "worker_step":
            style = {"tool": "yellow", "result": "dim", "text": "green", "think": "dim italic"}.get(p.get("step", ""), "")
            mark = {"tool": "⚙", "result": "  ↳", "text": "💬", "think": "…"}.get(p.get("step", ""), "·")
            return Text(f"{clock} {mark} {p.get('text', '')}", style=style)
        case "worker_end":
            ok = p.get("status") == "ok"
            return Text(f"{clock} ■ attempt {p.get('attempt')}: {p.get('status')}", style="green" if ok else "red")
        case "message":
            return Text(f"{clock} ✉ {p.get('from')} → {p.get('to')}: {p.get('body', '')[:400]}", style="bold blue")
        case "hint_applied":
            return Text(f"{clock} ✎ hint for {p.get('task_id')}: {p.get('text', '')}", style="bold magenta")
        case "run_cancel":
            return Text(f"{clock} ✗ run cancelled ({p.get('by', '')})", style="bold red")
        case "task_end":
            style = STATUS_STYLE.get(p.get("status", ""), "")
            return Text(f"{clock}   {STATUS_ICON.get(p.get('status', ''), '?')} {p.get('task_id')} {p.get('status')}",
                        style=style)
        case "run_end":
            return Text(f"{clock} ■ run {p.get('run_id')} {'OK' if p.get('ok') else 'with errors'}, "
                        f"branch {p.get('integration_branch')}", style="bold cyan" if p.get("ok") else "bold red")
        case "delegate":
            return Text(f"{clock} ⇒ delegate {ev.agent}: {p.get('status')} ({p.get('branch', '')}){tok}", style="magenta")
        case "role_attempt":
            return Text(f"{clock} ⚑ role {ev.agent}: {p.get('status', '')}{tok}", style="blue")
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
    BINDINGS = [Binding("q", "quit", "Quit"), Binding("n", "next_run", "Next run")]

    def __init__(self, store: EventStore, poll_s: float = 0.5):
        super().__init__()
        self.store, self.poll_s = store, poll_s
        self._last_id = 0
        self._events: list[Event] = []
        self._run_idx = -1  # -1: zawsze najnowszy run

    def compose(self) -> ComposeResult:
        yield Header()
        yield Static("Waiting for events...", id="head")
        yield DataTable(id="tasks", cursor_type="row")
        yield RichLog(id="feed", wrap=True, markup=False, highlight=False)
        yield Footer()

    def on_mount(self) -> None:
        self._head, self._table, self._feed = (self.query_one("#head", Static), self.query_one("#tasks", DataTable),
                                              self.query_one("#feed", RichLog))
        self._feed.can_focus = False
        for key, label in (("task", "task"), ("status", "status"), ("diff", "difficulty"), ("deps", "after"),
                           ("worker", "worker"), ("att", "attempts"), ("tok", "tokens in/out"), ("desc", "description")):
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
        state = ("OK" if run.ok else "finished with errors") if run.finished else "running"
        self._head.update(Text.assemble(
            (f"run {run.run_id}", "bold"), f"  {state}  {_elapsed(run)}  {done}/{len(run.tasks)} done  "
            f"branch {run.integration_branch}\n", (run.goal[:200], "italic")))
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
