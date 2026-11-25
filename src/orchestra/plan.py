"""Task plan as a DAG: data model (pydantic), validation and parsing of the planner's reply.

The `planner` role (e.g. Claude) returns the plan as JSON matching `Plan.model_json_schema()`.
Dependencies are checked with graphlib (stdlib), so cycles and missing dependencies surface immediately.
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
    """The planner's reply is not a valid plan."""


class PlanTask(BaseModel):
    id: str = Field(description="Short unique identifier: lowercase letters, digits, - or _")
    description: str = Field(min_length=1, description="Exactly what to implement, self-contained")
    files: list[str] = Field(default_factory=list, description="Files to change or create (relative paths)")
    acceptance: list[str] = Field(min_length=1, description="Acceptance criteria, each verifiable by a test")
    test_cmd: str = Field(min_length=1, description='Test command without shell, e.g. "pytest -q tests/test_x.py"')
    cwd: str = Field(default="", description="Repo subdirectory where the worker and test_cmd run (e.g. \"code\"); empty means repo root")
    difficulty: Difficulty = Field(default="easy", description="easy and medium go to the cheap worker, hard to the strong model")
    depends_on: list[str] = Field(default_factory=list, description="Task ids that must be done first")
    expect_red: bool = Field(default=True, description=(
        "Whether test_cmd must fail before the worker's changes (red check). Set false only for refactors and "
        "characterization tests, where the test already passes at the start and must still pass after the change"))

    @field_validator("id")
    @classmethod
    def _id_ok(cls, v: str) -> str:
        if not _ID.match(v):
            raise ValueError(f"invalid task id: {v!r}")
        return v

    @field_validator("test_cmd")
    @classmethod
    def _test_cmd_ok(cls, v: str) -> str:
        try:
            if not shlex.split(v):
                raise ValueError("empty test command")
        except ValueError as exc:
            raise ValueError(f"test_cmd cannot be split by shlex: {exc}") from exc
        return v

    @field_validator("cwd")
    @classmethod
    def _cwd_relative(cls, v: str) -> str:
        if v.startswith(("/", "~")) or ".." in v.replace("\\", "/").split("/"):
            raise ValueError(f"cwd must be relative and inside the repo: {v!r}")
        return v

    @field_validator("files")
    @classmethod
    def _files_relative(cls, v: list[str]) -> list[str]:
        for f in v:
            if f.startswith(("/", "~")) or ".." in f.replace("\\", "/").split("/"):
                raise ValueError(f"path must be relative and inside the repo: {f!r}")
        return v


class Plan(BaseModel):
    goal: str = Field(min_length=1)
    tasks: list[PlanTask] = Field(min_length=1)

    @model_validator(mode="after")
    def _graph_ok(self) -> "Plan":
        ids = [t.id for t in self.tasks]
        dup = {i for i in ids if ids.count(i) > 1}
        if dup:
            raise ValueError(f"duplicate task ids: {sorted(dup)}")
        known = set(ids)
        for t in self.tasks:
            missing = [d for d in t.depends_on if d not in known]
            if missing:
                raise ValueError(f"task {t.id} depends on unknown tasks: {missing}")
            if t.id in t.depends_on:
                raise ValueError(f"task {t.id} depends on itself")
        try:
            TopologicalSorter({t.id: set(t.depends_on) for t in self.tasks}).prepare()
        except CycleError as exc:
            raise ValueError(f"cycle in dependencies: {exc.args[1]}") from exc
        return self

    def by_id(self) -> dict[str, PlanTask]:
        return {t.id: t for t in self.tasks}

    def order(self) -> list[str]:
        return list(TopologicalSorter({t.id: set(t.depends_on) for t in self.tasks}).static_order())


def plan_schema() -> dict[str, Any]:
    return Plan.model_json_schema()


def extract_json(text: str) -> Any:
    """Extracts the first JSON object from the model's reply (```json block or bare object)."""
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
    raise PlanError("no JSON object found in the reply")


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
    """Asks the planner for a plan and validates it; on error sends a correction (once by default)."""
    prompt = PLANNER_PROMPT.format(schema=json.dumps(plan_schema(), ensure_ascii=False), files=repo_files or "(unknown)", goal=goal)
    last_error = ""
    for attempt in range(retries + 1):
        reply = await ask(prompt if not last_error else
                          f"{prompt}\n\nYour previous answer was invalid ({last_error}). Return corrected JSON only.")
        try:
            return parse_plan(reply)
        except PlanError as exc:
            last_error = str(exc)
    raise PlanError(f"planner did not return a valid plan: {last_error}")
