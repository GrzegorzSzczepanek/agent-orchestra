import subprocess
from pathlib import Path

import pytest


@pytest.fixture
def git_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def g(*a: str) -> None:
        subprocess.run(["git", *a], cwd=repo, check=True, capture_output=True)

    g("init", "-q", "-b", "main")
    g("config", "user.email", "t@example.com")
    g("config", "user.name", "T")
    (repo / "app.py").write_text("def add(a, b):\n    return a - b\n")
    (repo / ".gitignore").write_text(".orchestra/\n")
    g("add", "-A")
    g("commit", "-q", "-m", "init")
    return repo
