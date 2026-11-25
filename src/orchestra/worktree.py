"""Helper operations on git worktrees (via the git CLI)."""

from __future__ import annotations

import asyncio
import os
from typing import Callable, Iterable, Mapping
import re
import uuid
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


# Variables without secrets that workers and test commands receive. The rest must be listed in env_passthrough.
# A proxy may contain a password in the URL, but without it the worker cannot connect to the model through the proxy.
SAFE_ENV = frozenset((
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "COLORTERM", "LANG", "LANGUAGE", "TZ", "TMPDIR",
    "XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME", "XDG_RUNTIME_DIR",
    "NVM_DIR", "VIRTUAL_ENV", "CONDA_PREFIX", "PYTHONDONTWRITEBYTECODE", "CLAUDE_CONFIG_DIR", "CODEX_HOME",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
))


def safe_env(passthrough: Iterable[str] = ()) -> dict[str, str]:
    """Trimmed environment of the child process: SAFE_ENV, LC_* and explicitly listed variables."""
    allowed = SAFE_ENV.union(passthrough)
    return {k: v for k, v in os.environ.items() if k in allowed or k.startswith("LC_")}


async def run(argv: list[str], cwd: str | Path, timeout: float | None = 60,
              on_line: Callable[[str], None] | None = None,
              env: Mapping[str, str] | None = None) -> tuple[int, str, str]:
    """Run a process. on_line receives successive stdout lines live (for example worker NDJSON).

    env=None inherits the whole environment (git); for workers and tests pass safe_env().
    """
    proc = await asyncio.create_subprocess_exec(
        *argv, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        limit=16 * 1024 * 1024,  # NDJSON frames can be long
        env=None if env is None else dict(env),
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
                except Exception:  # noqa: BLE001 - a preview error must not kill the worker
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
    except asyncio.CancelledError:  # cancelling a run also kills the worker process
        proc.kill()
        raise
    return proc.returncode or 0, out, err


async def git(args: list[str], cwd: str | Path, timeout: float | None = 60) -> str:
    code, out, err = await run(["git", *args], cwd, timeout)
    if code != 0:
        raise GitError(f"git {' '.join(args)} exited with code {code}: {err.strip()}")
    return out


@dataclass(frozen=True)
class Worktree:
    path: Path
    branch: str
    base: str


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:30] or "task"


def ensure_local_exclude(repo: str | Path, pattern: str = ".orchestra/") -> None:
    """Append .orchestra/ to .git/info/exclude (locally, without changing the user's repo) unless it is already ignored."""
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
    except Exception:  # noqa: BLE001 - ignoring is a convenience, not a condition for working
        pass


async def create_worktree(repo: str | Path, name: str = "task", base: str = "HEAD") -> Worktree:
    """Create a worktree on a new branch orchestra/<slug>-<id> in <repo>/.orchestra/worktrees."""
    repo = Path(repo).resolve()
    ensure_local_exclude(repo)
    base_sha = (await git(["rev-parse", base], repo)).strip()
    ident = f"{_slug(name)}-{uuid.uuid4().hex[:6]}"
    branch = f"orchestra/{ident}"
    path = repo / ".orchestra" / "worktrees" / ident
    path.parent.mkdir(parents=True, exist_ok=True)
    await git(["worktree", "add", "-b", branch, str(path), base_sha], repo)
    return Worktree(path=path, branch=branch, base=base_sha)


def _is_test_byproduct(path: str) -> bool:
    """Python bytecode written by running the tests: *.pyc files and anything inside __pycache__."""
    return path.endswith(".pyc") or "__pycache__" in path.split("/")[:-1]


async def remove_byproducts(workdir: str | Path) -> None:
    """Delete untracked Python bytecode under workdir so it never reaches the diff or the worker's commit.

    Tests write it into the worktree when the repository does not ignore it. Tracked files are untouched,
    and paths the repository already ignores are skipped because git add -A would skip them anyway.
    """
    workdir = Path(workdir)
    out = await git(["ls-files", "--others", "--exclude-standard", "-z"], workdir)
    targets = [workdir / rel for rel in out.split("\0") if rel and _is_test_byproduct(rel)]
    for target in targets:
        target.unlink(missing_ok=True)
    for directory in sorted({t.parent for t in targets}, key=lambda p: len(p.parts), reverse=True):
        while (directory != workdir and directory.name == "__pycache__"
               and directory.is_dir() and not any(directory.iterdir())):
            directory.rmdir()
            directory = directory.parent


async def diff(wt: Worktree) -> str:
    """Full diff against the base, including new files (stage -A in the worktree)."""
    await git(["add", "-A"], wt.path)
    return await git(["diff", "--cached", "--no-color", wt.base], wt.path)


async def remove_worktree(repo: str | Path, wt: Worktree) -> None:
    await git(["worktree", "remove", "--force", str(wt.path)], repo)
    await git(["branch", "-D", wt.branch], repo)


async def view_diff(path: str | Path) -> str:
    """Diff of uncommitted changes in the worktree against HEAD (new files via intent-to-add)."""
    await remove_byproducts(path)
    await git(["add", "-N", "."], path)
    return await git(["diff", "--no-color", "HEAD"], path)
