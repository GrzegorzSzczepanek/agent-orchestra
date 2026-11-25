#!/usr/bin/env python3
"""Check that files contain no Polish text, then run the given tests against this checkout.

Usage: check_english.py FILE... [--tests TEST_FILE...]
A line containing "i18n-ok" is skipped (intentional Polish input, e.g. a parser that accepts it).
"""

import os
import re
import subprocess
import sys
from pathlib import Path

POLISH = re.compile(
    r"[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]|\b(nie|jest|brak|zadani\w*|plik\w*|uruchom\w*|domyslnie|dla|lub|oraz|albo|przez|gdy|jako|"
    r"tylko|bez|przed|kazd\w*|repozytorium|nieznan\w*|uwaga|wartosci|podglad|tryb|wynik\w*|przebieg\w*|sie|cel)\b",
    re.IGNORECASE,
)
# red before the translation started, unrelated to it
KNOWN_RED = [
    "tests/test_orchestration_ux.py::test_scheduler_logs_live_steps_and_attempt_details",
]


def main(argv: list[str]) -> int:
    split = argv.index("--tests") if "--tests" in argv else len(argv)
    files, tests = argv[:split], argv[split + 1:]
    hits = [f"{f}:{n}: {line.strip()[:120]}"
            for f in files
            for n, line in enumerate(Path(f).read_text(encoding="utf-8").splitlines(), 1)
            if "i18n-ok" not in line and POLISH.search(line)]
    if hits:
        print(f"{len(hits)} lines still contain Polish:", *hits[:60], sep="\n")
        return 1
    if not tests:
        return 0
    cmd = [sys.executable, "-m", "pytest", "-q", *tests, *(f"--deselect={t}" for t in KNOWN_RED)]
    return subprocess.run(cmd, env={**os.environ, "PYTHONPATH": str(Path("src").resolve())}).returncode


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
