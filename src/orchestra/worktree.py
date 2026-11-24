"""Pomocnicze operacje na git worktree (przez git CLI)."""

from __future__ import annotations

import asyncio
from typing import Callable
import re
import uuid
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


async def run(argv: list[str], cwd: str | Path, timeout: float | None = 60,
              on_line: Callable[[str], None] | None = None) -> tuple[int, str, str]:
    """Uruchamia proces. on_line dostaje kolejne linie stdout na żywo (np. NDJSON workera)."""
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,  # ramki NDJSON bywają długie
    )

    async def read_stdout() -> str:
        assert proc.stdout is not None
        chunks: list[str] = []
        async for raw in proc.stdout:
            line = raw.decode(errors="replace")
            chunks.append(line)
            if on_line is not None:
                try:
                    on_line(line.rstrip("\n"))
                except Exception:  # noqa: BLE001 - błąd podglądu nie może zabić workera
                    pass
        return "".join(chunks)

    async def read_stderr() -> str:
        assert proc.stderr is not None
        return (await proc.stderr.read()).decode(errors="replace")

    async def collect() -> tuple[str, str]:
        out, err = await asyncio.gather(read_stdout(), read_stderr())
        await proc.wait()
        return out, err

    try:
        out, err = await asyncio.wait_for(collect(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise
    except asyncio.CancelledError:  # anulowanie przebiegu zabija też proces workera
        proc.kill()
        raise
    return proc.returncode or 0, out, err


async def git(args: list[str], cwd: str | Path, timeout: float | None = 60) -> str:
    code, out, err = await run(["git", *args], cwd, timeout)
    if code != 0:
        raise GitError(f"git {' '.join(args)} zakończył się kodem {code}: {err.strip()}")
    return out


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    base: str


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "task"


def ensure_local_exclude(repo: str | Path, pattern: str = ".orchestra/") -> None:
    """Dopisuje .orchestra/ do .git/info/exclude (lokalnie, bez zmian w repo użytkownika), chyba że już jest ignorowane."""
    import subprocess

    try:
        repo = Path(repo)
        gitignore = repo / ".gitignore"
        if gitignore.is_file() and pattern.rstrip("/") in gitignore.read_text(encoding="utf-8").split():
            return
        out = subprocess.run(["git", "rev-parse", "--git-path", "info/exclude"], cwd=repo, capture_output=True,
                             text=True, check=True).stdout.strip()
        path = Path(out) if Path(out).is_absolute() else repo / out
        existing = path.read_text(encoding="utf-8") if path.is_file() else ""
        if pattern in existing.split():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(existing + ("" if not existing or existing.endswith("\n") else "\n") + pattern + "\n", encoding="utf-8")
    except Exception:  # noqa: BLE001 - ignorowanie to wygoda, nie warunek działania
        pass


async def create_worktree(repo: str | Path, name: str = "task", base: str = "HEAD") -> Worktree:
    """Tworzy worktree na nowej gałęzi orchestra/<slug>-<id> w <repo>/.orchestra/worktrees."""
    repo = Path(repo).resolve()
    ensure_local_exclude(repo)
    base_sha = (await git(["rev-parse", base], repo)).strip()
    ident = f"{_slug(name)}-{uuid.uuid4().hex[:6]}"
    branch = f"orchestra/{ident}"
    path = repo / ".orchestra" / "worktrees" / ident
    path.parent.mkdir(parents=True, exist_ok=True)
    await git(["worktree", "add", "-b", branch, str(path), base_sha], repo)
    return Worktree(path=path, branch=branch, base=base_sha)


async def diff(wt: Worktree) -> str:
    """Pełny diff względem bazy, razem z nowymi plikami (stage -A w worktree)."""
    await git(["add", "-A"], wt.path)
    return await git(["diff", "--cached", "--no-color", wt.base], wt.path)


async def remove_worktree(repo: str | Path, wt: Worktree) -> None:
    await git(["worktree", "remove", "--force", str(wt.path)], repo)
    await git(["branch", "-D", wt.branch], repo)


async def view_diff(path: str | Path) -> str:
    """Diff niezatwierdzonych zmian w worktree względem HEAD (nowe pliki przez intent-to-add)."""
    await git(["add", "-N", "."], path)
    return await git(["diff", "--no-color", "HEAD"], path)
