"""Turns log events into lines to display in the TUI (a pure function, easy to test)."""

from __future__ import annotations

from rich.text import Text

from .store import Event

STATUS_BY_KIND = {
    "session_start": "idle",
    "prompt_start": "working",
    "prompt_end": "idle",
    "permission_request": "waiting",
    "permission_response": "working",
    "worker_step": "working",
    "worker_end": "idle",
    "error": "error",
    "session_end": "stopped",
}

STREAM_KINDS = ("agent_message", "agent_thought")


def render_events(events: list[Event], compact: bool = False) -> list[Text]:
    """Joins consecutive stream chunks (message/thought) into one line and formats the rest separately."""
    lines: list[Text] = []
    buf: list[str] = []
    buf_kind = ""

    def flush() -> None:
        nonlocal buf, buf_kind
        if buf:
            joined = "".join(buf)
            if buf_kind == "agent_thought":
                if not compact:
                    lines.append(Text("… " + joined, style="dim italic"))
            else:
                lines.append(Text(joined, style="green"))
        buf, buf_kind = [], ""

    for ev in events:
        if ev.kind in STREAM_KINDS:
            if buf_kind != ev.kind:
                flush()
                buf_kind = ev.kind
            buf.append(ev.payload.get("text", ""))
            continue
        flush()
        line = _render_one(ev, compact)
        if line is not None:
            lines.append(line)
    flush()
    return lines


def _render_one(ev: Event, compact: bool = False) -> Text | None:
    p = ev.payload
    match ev.kind:
        case "prompt_start":
            return Text("▶ " + p.get("text", ""), style="bold cyan")
        case "tool_call":
            return Text(f"⚙ {p.get('title', '')} ({p.get('kind') or 'tool'})", style="yellow")
        case "tool_update":
            return Text(f"  ↳ {p.get('status') or ''} {p.get('title') or ''}".rstrip(), style="yellow")
        case "permission_request":
            return Text(f"? permission: {p.get('title', '')}", style="bold magenta")
        case "permission_response":
            return Text(f"  → decision: {p.get('option_id') or 'denied'}", style="magenta")
        case "prompt_end":
            return Text(f"■ turn ended ({p.get('stop_reason')}), tokens {ev.tokens_in}/{ev.tokens_out}", style="blue")
        case "session_start":
            return Text(f"● session {ev.session} in {p.get('cwd', '')}", style="dim")
        case "session_end":
            return Text("○ session ended", style="dim")
        case "error":
            return Text(f"✗ error: {p.get('error', '')}", style="bold red")
        case "user_message" | "context" | "other":
            return None
        case _:
            from .watch import fmt_event  # orchestration events (run_plan, delegate) have their own format

            line = fmt_event(ev, compact)
            if line is None and not compact:
                return Text(f"{ev.kind}: {p}", style="dim")
            return line


def status_of(kind: str, current: str) -> str:
    return STATUS_BY_KIND.get(kind, current)
