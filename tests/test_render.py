from __future__ import annotations

import pytest

from orchestra.render import STATUS_BY_KIND, render_events, status_of
from orchestra.store import Event


def ev(kind: str, payload: dict | None = None, **kw) -> Event:
    data = dict(
        id=0,
        ts=0.0,
        agent="agent",
        session="session",
        kind=kind,
        payload=payload if payload is not None else {},
        tokens_in=0,
        tokens_out=0,
        cost=0.0,
    )
    data.update(kw)
    return Event(**data)


def test_empty_events() -> None:
    assert render_events([]) == []


def test_agent_message_fragments_joined() -> None:
    lines = render_events([
        ev("agent_message", {"text": "hello "}),
        ev("agent_message", {"text": "world"}),
    ])
    assert len(lines) == 1
    assert str(lines[0]) == "hello world"


def test_agent_thought_prefix() -> None:
    lines = render_events([ev("agent_thought", {"text": "hmm"})])
    assert len(lines) == 1
    assert str(lines[0]).startswith("… ")


def test_stream_kind_switch_splits_lines() -> None:
    lines = render_events([
        ev("agent_message", {"text": "a"}),
        ev("agent_thought", {"text": "b"}),
    ])
    assert len(lines) == 2
    assert str(lines[0]) == "a"
    assert str(lines[1]) == "… b"


def test_non_stream_event_breaks_joining() -> None:
    lines = render_events([
        ev("agent_message", {"text": "a"}),
        ev("tool_call", {"title": "Read", "kind": "read"}),
        ev("agent_message", {"text": "b"}),
    ])
    assert len(lines) == 3
    assert str(lines[0]) == "a"
    assert str(lines[2]) == "b"


def test_prompt_start_formatting() -> None:
    lines = render_events([ev("prompt_start", {"text": "do it"})])
    assert len(lines) == 1
    assert str(lines[0]).startswith("▶ ")
    assert "do it" in str(lines[0])


def test_tool_call_formatting() -> None:
    lines = render_events([ev("tool_call", {"title": "Read", "kind": "read"})])
    assert len(lines) == 1
    assert "Read" in str(lines[0])
    assert "read" in str(lines[0])


def test_tool_call_kind_fallback() -> None:
    lines = render_events([ev("tool_call", {"title": "Read", "kind": ""})])
    assert len(lines) == 1
    assert "(tool)" in str(lines[0])


def test_tool_update_formatting() -> None:
    lines = render_events([ev("tool_update", {"status": "running", "title": "Read"})])
    assert len(lines) == 1
    text = str(lines[0])
    assert text.startswith("  ↳ ")
    assert "running" in text
    assert "Read" in text


def test_permission_request_formatting() -> None:
    lines = render_events([ev("permission_request", {"title": "Write file"})])
    assert len(lines) == 1
    assert str(lines[0]).startswith("? uprawnienie: ")
    assert "Write file" in str(lines[0])


def test_permission_response_option_id() -> None:
    lines = render_events([ev("permission_response", {"option_id": "allow"})])
    assert len(lines) == 1
    assert "allow" in str(lines[0])


def test_permission_response_denied_fallback() -> None:
    lines = render_events([ev("permission_response", {})])
    assert len(lines) == 1
    assert "odmowa" in str(lines[0])


def test_prompt_end_formatting() -> None:
    lines = render_events([
        ev("prompt_end", {"stop_reason": "end_turn"}, tokens_in=12, tokens_out=34),
    ])
    assert len(lines) == 1
    text = str(lines[0])
    assert "end_turn" in text
    assert "12/34" in text


def test_session_start_formatting() -> None:
    lines = render_events([ev("session_start", {"cwd": "/tmp/work"}, session="sess-1")])
    assert len(lines) == 1
    text = str(lines[0])
    assert "sess-1" in text
    assert "/tmp/work" in text


def test_session_end_formatting() -> None:
    lines = render_events([ev("session_end")])
    assert len(lines) == 1
    assert str(lines[0]) == "○ sesja zakończona"


def test_error_formatting() -> None:
    lines = render_events([ev("error", {"error": "boom"})])
    assert len(lines) == 1
    assert str(lines[0]).startswith("✗ błąd: ")
    assert "boom" in str(lines[0])


@pytest.mark.parametrize("kind", ["user_message", "context", "other"])
def test_skipped_kinds_render_nothing(kind: str) -> None:
    assert render_events([ev(kind, {"text": "ignored"})]) == []


def test_unknown_kind_renders_line_without_raising() -> None:
    lines = render_events([ev("mystery_kind", {"x": 1})])
    assert len(lines) == 1
    assert "mystery_kind" in str(lines[0])


@pytest.mark.parametrize("kind,expected", list(STATUS_BY_KIND.items()))
def test_status_of_known_kinds(kind: str, expected: str) -> None:
    assert status_of(kind, "current") == expected


def test_status_of_unknown_kind_returns_current() -> None:
    assert status_of("mystery_kind", "current") == "current"
