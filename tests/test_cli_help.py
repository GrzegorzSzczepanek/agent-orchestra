"""Acceptance test for the English, example-driven `--help` of the `orchestra` command."""

import argparse
import re

import pytest

from orchestra.cli import build_parser, main

COMMANDS = ["doctor", "plan", "init-plan", "run", "watch", "roles", "ledger"]
POLISH = re.compile("[ąćęłńóśźżĄĆĘŁŃÓŚŹŻ]")  # i18n-ok


def help_of(capsys, *argv: str) -> str:
    with pytest.raises(SystemExit) as exc:
        main([*argv, "--help"])
    assert exc.value.code == 0
    return capsys.readouterr().out


def test_top_level_help_is_a_guide(capsys, monkeypatch):
    monkeypatch.setenv("COLUMNS", "100")
    out = help_of(capsys)
    assert "Quick start:" in out and "Examples:" in out
    for name in COMMANDS:
        assert re.search(rf"^\s+{name}\s+\S", out, re.MULTILINE), name
    assert "orchestra doctor" in out and "orchestra run plan.json" in out
    assert "orchestra <command> --help" in out
    assert not POLISH.search(out)
    assert max(map(len, out.splitlines())) <= 100


@pytest.mark.parametrize("argv", [[c] for c in COMMANDS] + [["ledger", s] for s in ("add", "show", "budget")])
def test_every_command_has_description_examples_and_option_help(capsys, monkeypatch, argv):
    monkeypatch.setenv("COLUMNS", "100")
    out = help_of(capsys, *argv)
    assert "Examples:" in out and f"orchestra {' '.join(argv)}" in out.split("Examples:")[1]
    assert not POLISH.search(out)
    assert max(map(len, out.splitlines())) <= 100
    parser = build_parser()
    for name in argv:
        sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
        parser = sub.choices[name]
    assert parser.description
    assert [a.dest for a in parser._actions if not a.help] == []


def test_no_arguments_prints_help(capsys):
    assert main([]) == 0
    assert "Quick start:" in capsys.readouterr().out
