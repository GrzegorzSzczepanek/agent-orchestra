"""Command Code (`cmd`) support: headless mode parser, exit codes, model list, secret masking.

Format verified on `cmd` 1.93.2 and in the docs (commandcode.ai/docs/headless):
- `cmd -p ... --output-format json` prints NDJSON: `{"type":"event","event":{...}}` frames
  (unknown `event.type` values must be ignored) and one final `{"type":"result", ...}` line.
- Result line: subtype (success | error | max_turns), usage, durationMs, finalText are always present;
  sessionId, stopReason are optional; error only when subtype=error.
- usage has keys inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens.
The parser also understands `claude -p --output-format json` output (fields `result`, `total_cost_usd`).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# Exit codes of -p mode (commandcode.ai/docs/reference/cli) mapped to delegate statuses.
EXIT_STATUS: dict[int, str] = {
    0: "ok",
    1: "worker_failed",
    3: "auth_failed",
    4: "permission_denied",
    5: "rate_limited",
    6: "connection_error",
    7: "server_error",
    8: "max_turns",
    9: "no_response",
    10: "insufficient_credits",
    130: "interrupted",
}

_ANSI = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]")


def strip_ansi(text: str) -> str:
    return _ANSI.sub("", text)


def status_for_exit_code(code: int | None) -> str:
    """Status for a worker exit code; unknown non-zero codes are worker_failed."""
    if code is None:
        return "worker_failed"
    return EXIT_STATUS.get(code, "ok" if code == 0 else "worker_failed")


# --- headless result ------------------------------------------------------------------------


@dataclass
class HeadlessResult:
    final_text: str = ""
    subtype: str = ""  # success | error | max_turns | "" when no result line
    error: str = ""
    session_id: str = ""
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)  # normalized: tokens_in, tokens_out, ...
    cost_usd: float = 0.0
    events: int = 0  # number of recognized event frames
    parse_errors: int = 0  # lines that look like JSON but could not be parsed
    found_result: bool = False
    is_error: bool = False  # claude sets this even with subtype=success (e.g. usage limit)


def normalize_usage(raw: Mapping[str, Any] | None) -> dict[str, int]:
    """Normalizes usage of different agents to tokens_in / tokens_out / cache_read / cache_write."""
    raw = raw or {}

    def pick(*keys: str) -> int:
        for k in keys:
            v = raw.get(k)
            if isinstance(v, (int, float)):
                return int(v)
        return 0

    return {
        "tokens_in": pick("inputTokens", "input_tokens", "prompt_tokens"),
        "tokens_out": pick("outputTokens", "output_tokens", "completion_tokens"),
        "cache_read": pick("cacheReadTokens", "cache_read_input_tokens"),
        "cache_write": pick("cacheWriteTokens", "cache_creation_input_tokens"),
    }


def parse_headless_output(stdout: str) -> HeadlessResult:
    """Tolerant NDJSON parser: ignores unknown frames, only counts bad lines."""
    res = HeadlessResult()
    for line in stdout.splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            res.parse_errors += 1
            continue
        if not isinstance(obj, dict):
            continue
        kind = obj.get("type")
        if kind == "event":
            res.events += 1
        elif kind == "result":
            res.found_result = True
            res.subtype = str(obj.get("subtype") or "")
            text = obj.get("finalText")
            if text is None:
                text = obj.get("result", "")
            res.final_text = text if isinstance(text, str) else str(text)
            err = obj.get("error")
            res.error = err if isinstance(err, str) else ("" if err is None else str(err))
            res.is_error = bool(obj.get("is_error"))
            if res.is_error and not res.error:
                res.error = res.final_text
            res.session_id = str(obj.get("sessionId") or obj.get("session_id") or "")
            res.stop_reason = str(obj.get("stopReason") or "")
            usage = obj.get("usage")
            res.usage = normalize_usage(usage if isinstance(usage, dict) else None)
            cost = obj.get("total_cost_usd", obj.get("costUsd", 0.0))
            res.cost_usd = float(cost) if isinstance(cost, (int, float)) else 0.0
            # a later result line overwrites the previous one: the last wins
    return res


# --- model list ---------------------------------------------------------------------------

_MODEL_LINE = re.compile(r"^(?P<id>[A-Za-z0-9][\w.:/+-]*)\s{2,}(?P<desc>\S.*)$")


@dataclass(frozen=True)
class ModelInfo:
    id: str
    description: str
    section: str = ""


def parse_models(text: str) -> list[ModelInfo]:
    """Parses `cmd --list-models` output (sections and `id   description` rows).

    Helper rows ("Docs:  https://...", "cmd --model x") and the "Decision models (headless only)" section
    are skipped, so the model count matches the header (105 in cmd 1.93.2).
    """
    models: list[ModelInfo] = []
    section = ""
    for raw in strip_ansi(text).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _MODEL_LINE.match(line.strip())
        if section.lower().startswith("decision models"):
            continue  # special headless-only models (e.g. typesafe/jev), not suitable as a coding worker
        if m and not m["id"].endswith(":") and "://" not in m["id"]:
            models.append(ModelInfo(m["id"], m["desc"].strip(), section))
        elif not line.startswith(" ") and "  " not in line.strip():
            section = line.strip()
    return models


def resolve_model(wanted: str, models: Iterable[ModelInfo]) -> str | None:
    """Per the docs: full ID or the part after the last "/", case-insensitive."""
    w = wanted.strip().lower()
    if not w:
        return None
    for m in models:
        if m.id.lower() == w:
            return m.id
    for m in models:
        if m.id.lower().rsplit("/", 1)[-1] == w:
            return m.id
    return None


# --- secrets ------------------------------------------------------------------------------

_SECRET_NAME = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH)", re.I)
_SECRET_PATTERNS = [
    re.compile(r"\b(sk|pk|ak|ghp|gho|ghs|github_pat|xox[abpr])[-_][A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)[^\s\"',}]{6,}"),
]


def mask_secrets(text: str, env: Mapping[str, str] | None = None) -> str:
    """Masks environment variable values that look like secrets and common token patterns."""
    env = os.environ if env is None else env
    for name, value in env.items():
        if value and len(value) >= 6 and _SECRET_NAME.search(name):
            text = text.replace(value, f"***{name}***")
    for pat in _SECRET_PATTERNS:
        if pat.groups >= 2:
            text = pat.sub(lambda m: f"{m.group(1)}{m.group(2)}***", text)
        else:
            text = pat.sub("***", text)
    return text


def _short(value: Any, limit: int = 160) -> str:
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, default=str)
    text = " ".join(mask_secrets(text).split())  # mask before truncating, otherwise the start of a secret remains
    return text if len(text) <= limit else text[: limit - 1] + "…"


def summarize_frame(line: str) -> tuple[str, str] | None:
    """Short summary of an NDJSON frame from `cmd -p` for live preview: (kind, text) or None for noise.

    Kinds: tool (tool call), result (tool result), text (reply), think (reasoning).
    Frames known from cmd 1.93.2: tool_queued, tool_completed, message_end, thinking_end.
    """
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        frame = json.loads(line)
    except json.JSONDecodeError:
        return None
    ev = frame.get("event") if frame.get("type") == "event" else None
    if not isinstance(ev, dict):
        return None
    t = ev.get("type")
    if t == "tool_queued":
        inp = ev.get("input") or {}
        detail = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("path") or inp
        return "tool", f"{ev.get('toolName', '?')}: {_short(detail)}"
    if t == "tool_completed":
        res = ev.get("result")
        text = " ".join(str(r.get("text", "")) for r in res if isinstance(r, dict)) if isinstance(res, list) else res
        return "result", _short(text or "(no result)")
    if t == "message_end":
        parts = [c.get("text", "") for c in ev.get("content", []) if isinstance(c, dict) and c.get("type") == "text"]
        text = " ".join(p for p in parts if p).strip()
        return ("text", _short(text, 300)) if text else None
    if t == "thinking_end":
        return "think", _short(ev.get("text", ""), 200)
    return None
