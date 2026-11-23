"""Obsługa Command Code (`cmd`): parser trybu headless, kody wyjścia, lista modeli, maskowanie sekretów.

Format zweryfikowany na `cmd` 1.93.2 oraz w dokumentacji (commandcode.ai/docs/headless):
- `cmd -p ... --output-format json` wypisuje NDJSON: ramki `{"type":"event","event":{...}}`
  (nieznane `event.type` należy ignorować) i jedną końcową linię `{"type":"result", ...}`.
- Linia wyniku: subtype (success | error | max_turns), usage, durationMs, finalText zawsze obecne;
  sessionId, stopReason opcjonalne; error tylko przy subtype=error.
- usage ma klucze inputTokens, outputTokens, cacheReadTokens, cacheWriteTokens.
Parser rozumie też wynik `claude -p --output-format json` (pole `result`, `total_cost_usd`).
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping

# Kody wyjścia trybu -p (commandcode.ai/docs/reference/cli) zamienione na statusy delegate.
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
    """Status dla kodu wyjścia workera; nieznane niezerowe kody to worker_failed."""
    if code is None:
        return "worker_failed"
    return EXIT_STATUS.get(code, "ok" if code == 0 else "worker_failed")


# --- wynik trybu headless -------------------------------------------------------------------


@dataclass
class HeadlessResult:
    final_text: str = ""
    subtype: str = ""  # success | error | max_turns | "" gdy brak linii wyniku
    error: str = ""
    session_id: str = ""
    stop_reason: str = ""
    usage: dict[str, int] = field(default_factory=dict)  # znormalizowane: tokens_in, tokens_out, ...
    cost_usd: float = 0.0
    events: int = 0  # liczba rozpoznanych ramek event
    parse_errors: int = 0  # linie wyglądające na JSON, których nie dało się sparsować
    found_result: bool = False


def normalize_usage(raw: Mapping[str, Any] | None) -> dict[str, int]:
    """Sprowadza usage różnych agentów do tokens_in / tokens_out / cache_read / cache_write."""
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
    """Tolerancyjny parser NDJSON: nieznane ramki ignoruje, błędne linie tylko zlicza."""
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
            if obj.get("is_error") and not res.error:
                res.error = res.final_text
            res.session_id = str(obj.get("sessionId") or obj.get("session_id") or "")
            res.stop_reason = str(obj.get("stopReason") or "")
            usage = obj.get("usage")
            res.usage = normalize_usage(usage if isinstance(usage, dict) else None)
            cost = obj.get("total_cost_usd", obj.get("costUsd", 0.0))
            res.cost_usd = float(cost) if isinstance(cost, (int, float)) else 0.0
            # kolejna linia result nadpisze poprzednią: ostatnia wygrywa
    return res


# --- lista modeli -------------------------------------------------------------------------

_MODEL_LINE = re.compile(r"^(?P<id>[A-Za-z0-9][\w.:/+-]*)\s{2,}(?P<desc>\S.*)$")


@dataclass(frozen=True)
class ModelInfo:
    id: str
    description: str
    section: str = ""


def parse_models(text: str) -> list[ModelInfo]:
    """Parsuje wyjście `cmd --list-models` (sekcje i wiersze `id   opis`).

    Wiersze pomocnicze ("Docs:  https://...", "cmd --model x") oraz sekcja "Decision models (headless only)"
    są pomijane, dzięki czemu liczba modeli zgadza się z nagłówkiem (105 w cmd 1.93.2).
    """
    models: list[ModelInfo] = []
    section = ""
    for raw in strip_ansi(text).splitlines():
        line = raw.rstrip()
        if not line.strip():
            continue
        m = _MODEL_LINE.match(line.strip())
        if section.lower().startswith("decision models"):
            continue  # specjalne modele tylko dla headless (np. typesafe/jev), nie nadają się na workera kodującego
        if m and not m["id"].endswith(":") and "://" not in m["id"]:
            models.append(ModelInfo(m["id"], m["desc"].strip(), section))
        elif not line.startswith(" ") and "  " not in line.strip():
            section = line.strip()
    return models


def resolve_model(wanted: str, models: Iterable[ModelInfo]) -> str | None:
    """Zgodnie z dokumentacją: pełne ID albo część po ostatnim "/", bez rozróżniania wielkości liter."""
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


# --- sekrety ------------------------------------------------------------------------------

_SECRET_NAME = re.compile(r"(API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|AUTH)", re.I)
_SECRET_PATTERNS = [
    re.compile(r"\b(sk|pk|ak|ghp|gho|ghs|github_pat|xox[abpr])[-_][A-Za-z0-9_\-]{12,}"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{12,}"),
    re.compile(r"(?i)(api[_-]?key|token|secret|password)(\"?\s*[:=]\s*\"?)[^\s\"',}]{6,}"),
]


def mask_secrets(text: str, env: Mapping[str, str] | None = None) -> str:
    """Maskuje wartości zmiennych środowiskowych wyglądających na sekrety i typowe wzorce tokenów."""
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
