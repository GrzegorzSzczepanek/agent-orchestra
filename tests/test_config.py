from pathlib import Path

from orchestra.config import load_config

ROOT = Path(__file__).resolve().parent.parent


def test_load_repo_config():
    cfg = load_config(ROOT / "agents.toml")
    assert set(cfg.agents) >= {"claude", "codex", "antigravity", "opencode", "commandcode"}
    assert cfg.roles["implementer"] == "commandcode"  # agent_for_role depends on the installed binaries
    assert cfg.agents["commandcode"].acp == ("cmd", "acp")


def test_headless_argv_substitution():
    cfg = load_config(ROOT / "agents.toml")
    argv = cfg.agents["commandcode"].headless_argv("do X", model="m1")
    assert argv[:3] == ["cmd", "-p", "do X"]
    assert argv[argv.index("--model") + 1] == "m1"


def test_commandcode_requires_model_with_clear_message(monkeypatch):
    from dataclasses import replace

    import pytest

    from orchestra.config import ModelRequired

    monkeypatch.delenv("ORCHESTRA_CMD_MODEL", raising=False)
    cfg = load_config(ROOT / "agents.toml")
    assert cfg.agents["commandcode"].default_model  # the repo has a default model, so a missing variable is not an error
    bare = replace(cfg.agents["commandcode"], default_model="")
    with pytest.raises(ModelRequired, match="ORCHESTRA_CMD_MODEL"):
        bare.headless_argv("t")
    # environment variable, then the argument (the argument wins)
    monkeypatch.setenv("ORCHESTRA_CMD_MODEL", "z-ai/glm-5.3-flash")
    argv = cfg.agents["commandcode"].headless_argv("t")
    assert argv[argv.index("--model") + 1] == "z-ai/glm-5.3-flash"
    argv = cfg.agents["commandcode"].headless_argv("t", "kimi-k2.5")
    assert argv[argv.index("--model") + 1] == "kimi-k2.5"
    assert "--output-format" in argv and "json" in argv
    # an agent without {model} and without default_model gets no --model flag
    from orchestra.config import AgentConfig

    a = AgentConfig(name="x", headless=("x", "--model", "{model}", "{task}"))
    assert a.headless_argv("t") == ["x", "t"]


def test_skills_symlink():
    assert (ROOT / ".claude" / "skills").is_symlink()
    assert (ROOT / ".claude/skills/delegate-simple-tasks/SKILL.md").exists()
    assert (ROOT / "CLAUDE.md").read_text().strip() == "@AGENTS.md"
