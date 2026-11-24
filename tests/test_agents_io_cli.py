import json
import sys
from pathlib import Path

import pytest

from orchestra.agents_io import acp_ask, headless_ask, make_reviewer
from orchestra.cli import main
from orchestra.config import AgentConfig, Config
from orchestra.delegate import DelegateResult
from orchestra.plan import PlanTask
from orchestra.store import EventStore

PLAN = {"goal": "cel", "tasks": [
    {"id": "a", "description": "zrób a", "files": ["a.py"], "acceptance": ["a"], "test_cmd": f"{sys.executable} -m pytest -q test_a.py"},
    {"id": "b", "description": "zrób b", "files": ["b.py"], "acceptance": ["b"], "test_cmd": f"{sys.executable} -m pytest -q test_b.py",
     "depends_on": ["a"]},
]}

PLANNER = f"""import json, sys
plan = {PLAN!r}
print(json.dumps({{"type": "result", "subtype": "success", "usage": {{}}, "durationMs": 1,
                   "finalText": "Oto plan:\\n```json\\n" + json.dumps(plan) + "\\n```"}}))
"""

# worker: z promptu bierze id zadania i zapisuje implementację oraz test
WORKER = """import json, re, sys
tid = re.search(r"Task `([a-z0-9_-]+)`", sys.argv[1]).group(1)
open(tid + ".py", "w").write("VALUE = %r\\n" % tid)
open("test_" + tid + ".py", "w").write("from %s import VALUE\\n\\ndef test_x():\\n    assert VALUE == %r\\n" % (tid, tid))
print(json.dumps({"type": "result", "subtype": "success", "usage": {"inputTokens": 50, "outputTokens": 5},
                  "durationMs": 1, "finalText": "ok"}))
"""


def write_cfg(tmp: Path) -> Path:
    (tmp / "planner.py").write_text(PLANNER)
    (tmp / "worker.py").write_text(WORKER)
    cfg = tmp / "agents.toml"
    cfg.write_text(f"""
[roles]
planner = "planner"
reviewer = "planner"
implementer = "worker"

[agents.planner]
headless = ["{sys.executable}", "{tmp / 'planner.py'}", "{{task}}"]
acp = ["{sys.executable}", "-m", "orchestra.testing.mock_agent"]
tier = "strong"

[agents.worker]
headless = ["{sys.executable}", "{tmp / 'worker.py'}", "{{task}}"]
tier = "cheap"
""")
    return cfg


async def test_headless_ask_and_reviewer(git_repo: Path, tmp_path: Path):
    cfg_path = write_cfg(tmp_path)
    from orchestra.config import load_config

    cfg = load_config(cfg_path)
    reply = await headless_ask(cfg, "planner", git_repo)("daj plan")
    assert "```json" in reply

    async def ask(prompt: str) -> str:
        assert "task `a`" in prompt and "+x" in prompt
        return "CHANGES: dodaj docstring"

    verdict = await make_reviewer(ask)(PlanTask(id="a", description="d", acceptance=["x"], test_cmd="pytest"),
                                        DelegateResult(status="ok", diff="+x"))
    assert not verdict.approved and verdict.feedback == "dodaj docstring"

    async def approve(prompt: str) -> str:
        return "APPROVE"

    assert (await make_reviewer(approve)(PlanTask(id="a", description="d", acceptance=["x"], test_cmd="pytest"),
                                          DelegateResult(status="ok"))).approved


async def test_acp_ask_with_mock_agent(git_repo: Path, tmp_path: Path):
    from orchestra.config import load_config

    cfg = load_config(write_cfg(tmp_path))
    store = EventStore(":memory:")
    text = await acp_ask(cfg, "planner", git_repo, store)("hej")
    assert "hej" in text  # mock powtarza prompt


async def test_headless_ask_error_is_reported(git_repo: Path):
    from orchestra.agents_io import AskError

    bad = AgentConfig(name="b", headless=(sys.executable, "-c", "import sys; sys.exit(3)", "{task}"))
    with pytest.raises(AskError):
        await headless_ask(Config(agents={"b": bad}, roles={"planner": "b"}), "planner", git_repo)("x")


async def test_headless_ask_claude_is_error_with_zero_exit(git_repo: Path, tmp_path: Path):
    """Claude przy limicie zwraca kod 0, subtype=success i is_error=true; to nadal błąd roli."""
    from orchestra.agents_io import AskError

    script = tmp_path / "claude.py"
    fixture = Path(__file__).parent / "fixtures" / "claude_result_usage_limit.json"
    script.write_text(f"import sys\nsys.stdout.write(open({str(fixture)!r}).read())\n")
    agent = AgentConfig(name="c", headless=(sys.executable, str(script), "{task}"))
    with pytest.raises(AskError, match="monthly spend limit"):
        await headless_ask(Config(agents={"c": agent}, roles={"planner": "c"}), "planner", git_repo)("x")


def test_cli_plan_then_run_end_to_end(git_repo: Path, tmp_path: Path, capsys, monkeypatch):
    cfg = write_cfg(tmp_path)
    plan_path = tmp_path / "plan.json"
    assert main(["plan", "zrób a i b", "--config", str(cfg), "--repo", str(git_repo), "--out", str(plan_path)]) == 0
    assert "2 zadań" in capsys.readouterr().out
    plan = json.loads(plan_path.read_text())
    assert [t["id"] for t in plan["tasks"]] == ["a", "b"]

    report_path = tmp_path / "report.json"
    rc = main(["run", str(plan_path), "--config", str(cfg), "--repo", str(git_repo), "--report", str(report_path),
               "--parallel", "2"])
    out = capsys.readouterr().out
    assert rc == 0, out
    data = json.loads(report_path.read_text())
    assert data["ok"] and data["outcomes"]["a"]["status"] == "done" and data["outcomes"]["b"]["status"] == "done"
    assert data["outcomes"]["a"]["attempts"][0]["tokens_in"] == 50
    assert "git log --oneline" in out
    # zużycie widać w logu SQLite
    store = EventStore(git_repo / ".orchestra" / "events.db")
    assert store.usage_for_task(data["run_id"], "a")["tokens_in"] == 50


def test_cli_plan_fails_cleanly_on_bad_plan(git_repo: Path, tmp_path: Path, capsys):
    cfg = write_cfg(tmp_path)
    (tmp_path / "planner.py").write_text(
        'import json\nprint(json.dumps({"type":"result","subtype":"success","usage":{},"durationMs":1,"finalText":"nie json"}))\n')
    rc = main(["plan", "cel", "--config", str(cfg), "--repo", str(git_repo), "--out", str(tmp_path / "p.json")])
    assert rc == 1 and "Błąd planowania" in capsys.readouterr().err
