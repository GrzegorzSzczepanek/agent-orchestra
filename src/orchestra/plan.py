"""Plan zadań jako DAG: model danych (pydantic), walidacja i parsowanie odpowiedzi planisty.

Plan zwraca rola `planner` (np. Claude) jako JSON zgodny z `Plan.model_json_schema()`.
Zależności są sprawdzane przez graphlib (stdlib), więc cykle i brakujące zależności wychodzą od razu.
"""

from __future__ import annotations

import json
import re
import shlex
from graphlib import CycleError, TopologicalSorter
from typing import Any, Awaitable, Callable, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

Difficulty = Literal["easy", "medium", "hard"]
_ID = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$")


class PlanError(ValueError):
    """Odpowiedź planisty nie jest poprawnym planem."""


class PlanTask(BaseModel):
    id: str = Field(description="Krótki unikalny identyfikator: małe litery, cyfry, - lub _")
    description: str = Field(min_length=1, description="Co dokładnie zaimplementować, samowystarczalnie")
    files: list[str] = Field(default_factory=list, description="Pliki do zmiany lub utworzenia (ścieżki względne)")
    acceptance: list[str] = Field(min_length=1, description="Kryteria akceptacji, każde sprawdzalne testem")
    test_cmd: str = Field(min_length=1, description='Komenda testowa bez powłoki, np. "pytest -q tests/test_x.py"')
    cwd: str = Field(default="", description="Podkatalog repo, w którym działa worker i test_cmd (np. \"code\"); pusty to korzeń repo")
    difficulty: Difficulty = Field(default="easy", description="easy i medium robi tani worker, hard mocny model")
    depends_on: list[str] = Field(default_factory=list, description="Identyfikatory zadań, które muszą być gotowe")

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError(f"niepoprawne id zadania: {v!r}")
        return v

    @field_validator("test_cmd")
    @classmethod
    def _test_cmd_ok(cls, v: str) -> str:
        try:
            if not shlex.split(v):
                raise ValueError("pusta komenda testowa")
        except ValueError as exc:
            raise ValueError(f"test_cmd nie daje się rozbić przez shlex: {exc}") from exc
        return v

    @field_validator("cwd")
    @classmethod
    def _cwd_relative(cls, v: str) -> str:
        if v.startswith(("/", "~")) or ".." in v.replace("\\", "/").split("/"):
            raise ValueError(f"cwd musi być względny i wewnątrz repo: {v!r}")
        return v

    @field_validator("files")
    @classmethod
    def _files_relative(cls, v: list[str]) -> list[str]:
        for f in v:
            if f.startswith(("/", "~")) or ".." in f.replace("\\", "/").split("/"):
                raise ValueError(f"ścieżka musi być względna i wewnątrz repo: {f!r}")
        return v


class Plan(BaseModel):
    goal: str = Field(min_length=1)
    tasks: list[PlanTask] = Field(min_length=1)

    @model_validator(mode="after")
    def _graph_ok(self) -> "Plan":
        ids = [t.id for t in self.tasks]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"powtórzone id zadań: {sorted(dup)}")
        known = set(ids)
        for t in self.tasks:
            missing = [d for d in t.depends_on if d not in known]
            if missing:
                raise ValueError(f"zadanie {t.id} zależy od nieznanych zadań: {missing}")
            if t.id in t.depends_on:
                raise ValueError(f"zadanie {t.id} zależy samo od siebie")
        try:
            TopologicalSorter({t.id: set(t.depends_on) for t in self.tasks}).prepare()
        except CycleError as exc:
            raise ValueError(f"cykl w zależnościach: {exc.args[1]}") from exc
        return self

    def by_id(self) -> dict[str, PlanTask]:
        return {t.id: t for t in self.tasks}

    def order(self) -> list[str]:
        return list(TopologicalSorter({t.id: set(t.depends_on) for t in self.tasks}).static_order())


def plan_schema() -> dict[str, Any]:
    return Plan.model_json_schema()


def extract_json(text: str) -> Any:
    """Wyciąga pierwszy obiekt JSON z odpowiedzi modelu (blok ```json albo goły obiekt)."""
    decoder = json.JSONDecoder()
    candidates = [m.group(1) for m in re.finditer(r"```(?:json)?\s*(.*?)```", text, re.S)] + [text]
    for chunk in candidates:
        for m in re.finditer(r"\{", chunk):
            try:
                obj, _ = decoder.raw_decode(chunk[m.start():])
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                return obj
    raise PlanError("w odpowiedzi nie znaleziono obiektu JSON")


def parse_plan(text: str) -> Plan:
    obj = extract_json(text)
    try:
        return Plan.model_validate(obj)
    except ValidationError as exc:
        raise PlanError(_short_errors(exc)) from exc


def _short_errors(exc: ValidationError) -> str:
    parts = []
    for e in exc.errors()[:8]:
        loc = ".".join(str(x) for x in e["loc"])
        parts.append(f"{loc}: {e['msg']}")
    return "; ".join(parts)


PLANNER_PROMPT = """You are the planner in a cost-aware multi-agent coding setup.
Break the goal below into small, independently testable tasks. A cheap model will implement
"easy" and "medium" tasks, a strong model handles "hard" ones (design, ambiguity, cross-cutting changes).

Rules:
- Each task must be self-contained: the description alone is enough to implement it.
- Every task needs a test_cmd that fails before and passes after the change (no shell features, plain argv).
- Prefer 3-8 tasks. Use depends_on only when a task really needs another task's code.
- Touch as few files as possible per task. Paths are relative to the repository root.
- Reply with ONLY one JSON object matching this JSON Schema, no commentary:

{schema}

Repository files (truncated):
{files}

Goal:
{goal}
"""


async def make_plan(
    goal: str,
    ask: Callable[[str], Awaitable[str]],
    *,
    repo_files: str = "",
    retries: int = 1,
) -> Plan:
    """Prosi planistę o plan i waliduje go; przy błędzie wysyła poprawkę (domyślnie 1 raz)."""
    prompt = PLANNER_PROMPT.format(schema=json.dumps(plan_schema(), ensure_ascii=False), files=repo_files or "(unknown)", goal=goal)
    last_error = ""
    for attempt in range(retries + 1):
        reply = await ask(prompt if not last_error else
                          f"{prompt}\n\nYour previous answer was invalid ({last_error}). Return corrected JSON only.")
        try:
            return parse_plan(reply)
        except PlanError as exc:
            last_error = str(exc)
    raise PlanError(f"planista nie zwrócił poprawnego planu: {last_error}")
