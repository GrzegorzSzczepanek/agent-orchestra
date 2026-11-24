"""Wybór Node >= 22 dla agentów npm (cmd, claude-agent-acp), niezależnie od domyślnego Node w shellu."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
from pathlib import Path

MIN_MAJOR = 22


def _major(version: str) -> int:
    m = re.match(r"v?(\d+)", version.strip())
    return int(m.group(1)) if m else 0


def current_node_major(path: str | None = None) -> int:
    node = shutil.which("node", path=path)
    if not node:
        return 0
    try:
        out = subprocess.run([node, "-v"], capture_output=True, text=True, timeout=10).stdout
    except (OSError, subprocess.SubprocessError):
        return 0
    return _major(out)


def find_nvm_node(root: Path | None = None, minimum: int = MIN_MAJOR) -> Path | None:
    """Katalog bin najnowszej wersji Node >= minimum zainstalowanej przez nvm (z `cmd` w środku, jeśli się da)."""
    base = (root or Path(os.environ.get("NVM_DIR", Path.home() / ".nvm"))) / "versions" / "node"
    if not base.is_dir():
        return None
    versions = sorted((d for d in base.iterdir() if _major(d.name) >= minimum and (d / "bin" / "node").exists()),
                      key=lambda d: [int(x) for x in re.findall(r"\d+", d.name)], reverse=True)
    for d in versions:  # preferuj instalację, w której jest cmd
        if (d / "bin" / "cmd").exists():
            return d / "bin"
    return versions[0] / "bin" if versions else None


def ensure_node() -> str:
    """Gdy domyślny Node jest za stary albo `cmd` nie jest w PATH, dopisuje na początek PATH bin z nvm (Node >= 22).

    Zwraca dopisany katalog albo "".
    """
    if current_node_major() >= MIN_MAJOR and shutil.which("cmd"):
        return ""
    found = find_nvm_node()
    if found is None:
        return ""
    os.environ["PATH"] = f"{found}{os.pathsep}{os.environ.get('PATH', '')}"
    return str(found)
