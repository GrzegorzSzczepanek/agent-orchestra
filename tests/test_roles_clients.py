"""Fallback ról, wybór agentów (TUI, CLI), własni providerzy, klienci launchera, plan z TUI."""

import json
import subprocess
import sys
from pathlib import Path

import pytest
from textual.widgets import Input, Select

from orchestra.cli import main as cli_main
from orchestra.clients import NONE, build_launch, detect_client, skill_text
from orchestra.config import AgentConfig, Config, load_config, read_overrides, save_overrides
from orchestra.controller import Controller
from orchestra.store import EventStore
from orchestra.tui import OrchestraApp
from orchestra.worktree import ensure_local_exclude

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable


def agent(name, tier="strong", binary=PY, **kw):
    return AgentConfig(name=name, headless=(binary, "-c", "print(1)", "{task}"), tier=tier, **kw)


def cfg3(**kw):
    return Config(agents={"gone": agent("gone", binary="no-such-binary-xyz"), "big": agent("big", "strong"),
                          "small": agent("small", "cheap")},
                  roles={"planner": "gone", "reviewer": "gone", "implementer": "gone"}, **kw)


def test_missing_agent_falls_back_by_tier():
    cfg = cfg3()
    assert cfg.role_info("planner").effective == "big"  # planner woli mocny
    assert cfg.role_info("implementer").effective == "small"  # worker woli tani
    assert cfg.role_info("planner").changed and "brak gone" in cfg.role_info("planner").reason
    assert cfg.agent_for_role("reviewer").name == "big"


def test_fallback_order_and_available_agent_untouched():
    cfg = cfg3(fallback_order=("small", "big"))
    assert cfg.role_name("planner") == "big"  # kolejność nie łamie preferencji tieru
    cfg.roles["planner"] = "small"
    assert cfg.role_name("planner") == "small"  # zainstalowany agent z agents.toml zostaje


def test_nothing_installed_keeps_configured_name_and_says_why():
    cfg = Config(agents={"gone": agent("gone", binary="no-such-binary-xyz")}, roles={"planner": "gone"})
    info = cfg.role_info("planner")
    assert info.effective == "gone" and "żaden agent" in info.reason


def test_user_override_wins_and_model_override_beats_env(tmp_path, monkeypatch):
    monkeypatch.setenv("M_ENV", "z-env")
    cfg = Config(agents={"a": agent("a", model_env="M_ENV", default_model="d"), "b": agent("b")}, roles={"planner": "a"})
    assert cfg.agents["a"].resolve_model() == "z-env"
    save_overrides(tmp_path, roles={"planner": "b", "bogus": "nie-ma"}, models={"a": "z-tui"})
    cfg.reload_overrides(tmp_path)
    assert cfg.role_name("planner") == "b" and cfg.role_info("planner").reason == "wybór użytkownika"
    assert cfg.agents["a"].resolve_model() == "z-tui"
    save_overrides(tmp_path, roles={"planner": ""}, models={"a": ""})  # pusta wartość kasuje wybór
    cfg.reload_overrides(tmp_path)
    assert cfg.role_name("planner") == "a" and cfg.agents["a"].resolve_model() == "z-env"
    assert read_overrides(tmp_path)[0] == {"bogus": "nie-ma"}


def test_broken_overrides_file_is_ignored(tmp_path):
    (tmp_path / ".orchestra").mkdir()
    (tmp_path / ".orchestra" / "roles.json").write_text("{nie json")
    assert read_overrides(tmp_path) == ({}, {})


def test_custom_provider_from_user_and_repo_config(tmp_path, monkeypatch):
    user = tmp_path / "user.toml"
    user.write_text('[agents.moj]\nheadless = ["mojcli", "run", "{task}", "--m", "{model}"]\ntier = "cheap"\n'
                    'default_model = "lokalny/llama"\n[roles]\nimplementer = "moj"\n')
    monkeypatch.setenv("ORCHESTRA_USER_CONFIG", str(user))
    repo = tmp_path / "r"
    (repo / ".orchestra").mkdir(parents=True)
    (repo / ".orchestra" / "agents.toml").write_text('fallback_order = ["moj"]\n[agents.drugi]\nheadless = ["x", "{task}"]\n')
    cfg = load_config(ROOT / "agents.toml", repo=repo)
    assert {"claude", "moj", "drugi"} <= set(cfg.agents)
    assert cfg.roles["implementer"] == "moj" and cfg.fallback_order == ("moj",)
    assert cfg.agents["moj"].headless_argv("t")[-1] == "lokalny/llama"


def test_local_exclude_added_once_and_gitignore_respected(tmp_path):
    def mk(name):
        d = tmp_path / name
        d.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=d, check=True)
        return d

    a = mk("a")
    ensure_local_exclude(a)
    ensure_local_exclude(a)
    assert (a / ".git" / "info" / "exclude").read_text().split().count(".orchestra/") == 1
    (a / ".orchestra").mkdir()
    (a / ".orchestra" / "x").write_text("1")
    assert subprocess.run(["git", "status", "--porcelain"], cwd=a, capture_output=True, text=True).stdout == ""
    b = mk("b")
    (b / ".gitignore").write_text(".orchestra\n")
    ensure_local_exclude(b)
    assert ".orchestra" not in (b / ".git" / "info" / "exclude").read_text()


def test_cli_roles_set_and_init_plan(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_USER_CONFIG", str(tmp_path / "none.toml"))
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    base = ["roles", "--config", str(ROOT / "agents.toml"), "--repo", str(repo)]
    assert cli_main([*base, "--set", "planner=opencode"]) == 0
    out = capsys.readouterr().out
    assert "planner" in out and "opencode" in out
    assert read_overrides(repo)[0] == {"planner": "opencode"}
    assert cli_main([*base, "--set", "planner=nie-ma"]) == 2
    plan = tmp_path / "plan.json"
    assert cli_main(["init-plan", str(plan)]) == 0
    from orchestra.plan import Plan

    assert len(Plan.model_validate_json(plan.read_text()).tasks) == 2
    assert cli_main(["init-plan", str(plan)]) == 1  # nie nadpisuje


# --- klienci launchera ---
def test_detect_client_prefers_order_and_falls_back_to_none(monkeypatch, tmp_path):
    for name in ("opencode", "codex"):
        f = tmp_path / name
        f.write_text("#!/bin/sh\n")
        f.chmod(0o755)
    monkeypatch.setenv("PATH", str(tmp_path))
    assert detect_client() == "opencode"
    assert detect_client("codex") == "codex"
    assert detect_client(NONE) == NONE
    with pytest.raises(FileNotFoundError):
        detect_client("claude")
    with pytest.raises(ValueError):
        detect_client("emacs")
    monkeypatch.setenv("PATH", str(tmp_path / "pusto"))
    assert detect_client() == NONE


def test_skill_text_strips_frontmatter():
    text = skill_text(ROOT / ".agents" / "skills" / "orchestrate-hard-task" / "SKILL.md")
    assert text.startswith("# Splitting a hard task") and "name:" not in text.splitlines()[0]


def test_build_launch_per_client(tmp_path):
    repo, cfg = tmp_path, ROOT / "agents.toml"
    c = build_launch("claude", repo, cfg, "SKILL", ["--model", "x"])
    mcp = json.loads(c.argv[c.argv.index("--mcp-config") + 1])["mcpServers"]["orchestra"]
    assert mcp["env"]["ORCHESTRA_REPO"] == str(repo.resolve()) and "mcp__orchestra__run_plan" in c.argv
    assert c.argv[-2:] == ["--model", "x"]

    o = build_launch("opencode", repo, cfg, "SKILL")
    conf = json.loads(o.env["OPENCODE_CONFIG_CONTENT"])
    assert conf["mcp"]["orchestra"]["type"] == "local" and conf["mcp"]["orchestra"]["command"][0].endswith("orchestra-mcp")
    assert "SKILL" in Path(conf["instructions"][0]).read_text() and o.argv == ["opencode"]

    x = build_launch("codex", repo, cfg, "SKILL")
    joined = " ".join(x.argv)
    assert "mcp_servers.orchestra.command=" in joined and "mcp_servers.orchestra.env={ORCHESTRA_REPO=" in joined
    assert x.argv[0] == "codex" and "SKILL" in x.argv[-1]
    assert 'mcp_servers.orchestra.default_tools_approval_mode="approve"' in x.argv
    with pytest.raises(ValueError):
        build_launch("emacs", repo, cfg, "S")


def test_orch_script_syntax_and_no_hard_claude_requirement():
    text = (ROOT / "scripts" / "orch").read_text()
    assert subprocess.run(["bash", "-n", str(ROOT / "scripts" / "orch")]).returncode == 0
    assert "--client" in text and "command -v claude" not in text  # brak claude nie kończy się już błędem


# --- TUI ---
async def wait_for(pilot, cond, timeout=10.0):
    for _ in range(int(timeout / 0.1)):
        if cond():
            return
        await pilot.pause(0.1)
    raise AssertionError("timeout")


async def test_tui_roles_screen_saves_and_shows_fallback(git_repo):
    cfg = cfg3()
    store = EventStore(":memory:")
    app = OrchestraApp(store, Controller(store, []), poll_s=0.05, config=cfg, repo=git_repo)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.pause(0.2)
        bar = str(app._rolebar.render())
        assert "planner: big *" in bar and "implementer: small *" in bar
        await pilot.press("f8")
        await wait_for(pilot, lambda: bool(app.screen.query("#roles-ok")))
        screen = app.screen
        screen.query_one("#role-planner", Select).value = "small"
        screen.query_one("#model-agent", Select).value = "small"
        await pilot.pause(0.1)
        screen.query_one("#model-value", Input).value = "moj/model"
        await pilot.click("#roles-ok")
        await pilot.pause(0.4)
        assert read_overrides(git_repo) == ({"planner": "small", "reviewer": "big", "implementer": "small"},
                                            {"small": "moj/model"})
        bar = str(app._rolebar.render())
        assert "planner: small" in bar and "[moj/model]" in bar


async def test_tui_runs_plan_file_with_custom_scripted_worker(git_repo):
    """Bez żadnego klienta: własny plan z pliku i własny agent (skrypt) jako worker."""
    code = "open('hello.py','w').write('V = 1\\n')"
    worker = AgentConfig(name="skrypt", headless=(PY, "-c", code, "{task}"), tier="cheap")
    cfg = Config(agents={"skrypt": worker}, roles={"implementer": "skrypt", "planner": "skrypt"})
    plan = {"goal": "g", "tasks": [{"id": "hello", "description": "d", "files": ["hello.py"], "acceptance": ["jest"],
                                     "test_cmd": f"{PY} -c \"import hello; assert hello.V == 1\"", "difficulty": "easy"}]}
    (git_repo / "plan.json").write_text(json.dumps(plan))
    store = EventStore(":memory:")
    app = OrchestraApp(store, Controller(store, []), poll_s=0.05, config=cfg, repo=git_repo)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("f7")
        await wait_for(pilot, lambda: bool(app.screen.query("#plan-file-ok")))
        assert app.screen.query_one("#plan-file", Input).value == "plan.json"
        await pilot.click("#plan-file-ok")
        # czekamy na stan w aplikacji (TUI odczytuje log cyklicznie), a nie tylko na zdarzenie w bazie
        await wait_for(pilot, lambda: (r := app._latest_run()) is not None and r.finished, timeout=60)
        run = app._latest_run()
        assert run is not None and run.tasks["hello"].status == "done"
    branch = subprocess.run(["git", "branch", "--list", "orchestra/run-*"], cwd=git_repo, capture_output=True, text=True).stdout
    assert "orchestra/run-" in branch


async def test_tui_plan_screen_reports_bad_file(git_repo):
    store = EventStore(":memory:")
    app = OrchestraApp(store, Controller(store, []), poll_s=0.05, config=cfg3(), repo=git_repo)
    async with app.run_test(size=(130, 40)) as pilot:
        await pilot.press("f7")
        await wait_for(pilot, lambda: bool(app.screen.query("#plan-file-ok")))
        app.screen.query_one("#plan-file", Input).value = "nie-ma.json"
        await pilot.click("#plan-file-ok")
        await pilot.pause(0.5)
        assert not any(e.kind == "run_start" for e in store.events(limit=100))


async def test_missing_test_binary_stops_before_worker_spends_tokens(git_repo):
    from orchestra.delegate import delegate

    cfg = Config(agents={}, roles={"implementer": "fake"})
    res = await delegate(cfg, git_repo, "zrób coś", worker="fake", test_cmd="nie-ma-takiego-polecenia -q",
                         fake_files={"x.py": "1"})
    assert res.status == "config_error" and "nie-ma-takiego-polecenia" in res.error
    assert res.worker_text == "" and res.diff == ""  # worker w ogóle nie ruszył
    ok = await delegate(cfg, git_repo, "zrób coś", worker="fake", test_cmd=f"{PY} -c pass", fake_files={"x.py": "1"})
    assert ok.status == "ok"
    rel = await delegate(cfg, git_repo, "zrób coś", worker="fake", test_cmd="./brak.sh", fake_files={"x.py": "1"})
    assert rel.status == "config_error"
