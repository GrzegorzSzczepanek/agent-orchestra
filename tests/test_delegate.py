import sys
from pathlib import Path

from fastmcp import Client

from orchestra.config import AgentConfig, Config
from orchestra.delegate import delegate, parse_command_code_output
from orchestra.mcp_server import build_server

FIX = {"app.py": "def add(a, b):\n    return a + b\n"}
TEST = "test_app.py"
TEST_SRC = "from app import add\n\ndef test_add():\n    assert add(1, 2) == 3\n"


def cfg(**agents: AgentConfig) -> Config:
    return Config(agents=agents, roles={"implementer": "fake"})


async def test_fake_worker_ok(git_repo: Path):
    files = {**FIX, TEST: TEST_SRC}
    res = await delegate(cfg(), git_repo, "napraw add", worker="fake", fake_files=files,
                         test_cmd=f"{sys.executable} -m pytest -q {TEST}", keep_worktree=False)
    assert res.status == "ok", res
    assert res.test_exit_code == 0
    assert "+    return a + b" in res.diff and "test_app.py" in res.diff
    assert not Path(res.worktree).exists()
    # repo bazowe nietknięte
    assert "a - b" in (git_repo / "app.py").read_text()


async def test_fake_worker_tests_fail(git_repo: Path):
    res = await delegate(cfg(), git_repo, "nic nie rób", worker="fake",
                         fake_files={TEST: TEST_SRC}, test_cmd=f"{sys.executable} -m pytest -q {TEST}")
    assert res.status == "tests_failed"
    assert res.test_exit_code != 0
    assert Path(res.worktree).exists()  # keep_worktree domyślnie True


async def test_fake_path_traversal_blocked(git_repo: Path):
    res = await delegate(cfg(), git_repo, "x", worker="fake", fake_files={"../evil.txt": "x"})
    assert res.status == "worker_failed" and "poza worktree" in res.error


async def test_missing_binary(git_repo: Path):
    c = cfg(ghost=AgentConfig(name="ghost", headless=("definitely-not-installed", "{task}")))
    res = await delegate(c, git_repo, "x", worker="ghost")
    assert res.status == "worker_not_found"


async def test_headless_worker_script_parses_cmd_json(git_repo: Path, tmp_path: Path):
    script = tmp_path / "cmdlike.py"
    result = {"type": "result", "subtype": "success", "usage": {"inputTokens": 10, "outputTokens": 5},
              "durationMs": 1, "finalText": "gotowe"}
    script.write_text(
        "import sys, json\n"
        "open('app.py','w').write('def add(a, b):\\n    return a + b\\n')\n"
        "print(json.dumps({'type':'event','x':1}))\n"
        f"print(json.dumps({result!r}))\n"
    )
    c = cfg(cmdlike=AgentConfig(name="cmdlike", headless=(sys.executable, str(script), "{task}")))
    res = await delegate(c, git_repo, "napraw", worker="cmdlike")
    assert res.status == "ok"
    assert res.worker_text == "gotowe"
    assert res.usage == {"inputTokens": 10, "outputTokens": 5}
    assert "+    return a + b" in res.diff


async def test_worker_nonzero_exit(git_repo: Path, tmp_path: Path):
    script = tmp_path / "bad.py"
    script.write_text("import sys; sys.exit(4)\n")
    c = cfg(bad=AgentConfig(name="bad", headless=(sys.executable, str(script))))
    res = await delegate(c, git_repo, "x", worker="bad")
    assert res.status == "worker_failed" and res.worker_exit_code == 4


def test_parse_ignores_garbage():
    text, usage, sub = parse_command_code_output("nie json\n{zepsute\n")
    assert usage == {} and sub == ""


async def test_mcp_tool_end_to_end(git_repo: Path):
    server = build_server(config=cfg(), repo=git_repo)
    async with Client(server) as client:
        tools = await client.list_tools()
        assert [t.name for t in tools] == ["delegate"]
        r = await client.call_tool("delegate", {
            "prompt": "napraw", "worker": "fake", "fake_files": {**FIX, TEST: TEST_SRC},
            "test_cmd": f"{sys.executable} -m pytest -q {TEST}", "keep_worktree": False,
        })
        data = r.data
        assert data["status"] == "ok" and data["test_exit_code"] == 0


async def test_mcp_stdio_subprocess(git_repo: Path):
    """Prawdziwy serwer po stdio: tak rejestruje się go w Claude Code."""
    from fastmcp.client.transports import StdioTransport

    root = Path(__file__).resolve().parent.parent
    transport = StdioTransport(
        command=sys.executable, args=["-m", "orchestra.mcp_server"],
        env={"ORCHESTRA_CONFIG": str(root / "agents.toml"), "ORCHESTRA_REPO": str(git_repo)},
    )
    async with Client(transport) as client:
        r = await client.call_tool("delegate", {
            "prompt": "napraw", "worker": "fake", "fake_files": FIX, "keep_worktree": False})
        assert r.data["status"] == "ok" and "+    return a + b" in r.data["diff"]
