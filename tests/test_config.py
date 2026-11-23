from pathlib import Path

from orchestra.config import load_config

ROOT = Path(__file__).resolve().parent.parent


def test_load_repo_config():
    cfg = load_config(ROOT / "agents.toml")
    assert set(cfg.agents) >= {"claude", "codex", "antigravity", "opencode", "commandcode"}
    assert cfg.agent_for_role("implementer").name == "commandcode"
    assert cfg.agents["commandcode"].acp == ("cmd", "acp")


def test_headless_argv_substitution():
    cfg = load_config(ROOT / "agents.toml")
    argv = cfg.agents["commandcode"].headless_argv("zrób X", model="m1")
    assert argv[:3] == ["cmd", "-p", "zrób X"]
    assert argv[argv.index("--model") + 1] == "m1"


def test_headless_argv_default_model_and_missing_model():
    cfg = load_config(ROOT / "agents.toml")
    argv = cfg.agents["commandcode"].headless_argv("t")
    assert "deepseek/deepseek-v4.1-flash" in argv
    # agent bez {model} i bez default_model nie dostaje flagi --model
    from orchestra.config import AgentConfig

    a = AgentConfig(name="x", headless=("x", "--model", "{model}", "{task}"))
    assert a.headless_argv("t") == ["x", "t"]


def test_skills_symlink():
    assert (ROOT / ".claude" / "skills").is_symlink()
    assert (ROOT / ".claude/skills/delegate-simple-tasks/SKILL.md").exists()
    assert (ROOT / "CLAUDE.md").read_text().strip() == "@AGENTS.md"
