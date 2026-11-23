"""Pomocnicze operacje na git worktree (przez git CLI)."""

from __future__ import annotations

import asyncio
import re
import uuid
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


async def run(argv: list[str], cwd: str | Path, timeout: float | None = 60) -> tuple[int, str, str]:
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise
    return proc.returncode or 0, out.decode(errors="replace"), err.decode(errors="replace")


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


async def create_worktree(repo: str | Path, name: str = "task", base: str = "HEAD") -> Worktree:
    """Tworzy worktree na nowej gałęzi orchestra/<slug>-<id> w <repo>/.orchestra/worktrees."""
    repo = Path(repo).resolve()
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
