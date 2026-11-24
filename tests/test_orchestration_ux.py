import os
import sys
from pathlib import Path

from fastmcp import Client

from orchestra.config import AgentConfig, Config
from orchestra.mcp_server import build_server
from orchestra.node_env import ensure_node, find_nvm_node
from orchestra.progress import build_runs
from orchestra.store import EventStore
from orchestra.watch import WatchApp, fmt_event


def make_nvm(root: Path, versions: dict[str, bool]) -> Path:
    for v, has_cmd in versions.items():
        b = root / "versions" / "node" / v / "bin"
        b.mkdir(parents=True)
        (b / "node").write_text("")
        if has_cmd:
            (b / "cmd").write_text("")
    return root


def test_find_nvm_node_picks_newest_with_cmd(tmp_path: Path):
    root = make_nvm(tmp_path, {"v18.20.4": False, "v20.19.5": True, "v22.14.0": False, "v22.23.5": True})
    assert find_nvm_node(root) == root / "versions/node/v22.23.5/bin"


def test_find_nvm_node_falls_back_to_newest_without_cmd(tmp_path: Path):
    root = make_nvm(tmp_path, {"v20.19.5": True, "v22.14.0": False})
    assert find_nvm_node(root) == root / "versions/node/v22.14.0/bin"
    assert find_nvm_node(make_nvm(tmp_path / "x", {"v20.19.5": True})) is None


def test_ensure_node_noop_when_new_enough(monkeypatch):
    monkeypatch.setattr("orchestra.node_env.current_node_major", lambda path=None: 22)
    monkeypatch.setattr("orchestra.node_env.shutil.which", lambda name, path=None: "/x/cmd")
    before = os.environ["PATH"]
    assert ensure_node() == "" and os.environ["PATH"] == before


def test_ensure_node_adds_nvm_when_cmd_missing_on_new_node(monkeypatch, tmp_path: Path):
    b = make_nvm(tmp_path, {"v22.23.5": True}) / "versions/node/v22.23.5/bin"
    monkeypatch.setattr("orchestra.node_env.current_node_major", lambda path=None: 25)
    monkeypatch.setattr("orchestra.node_env.shutil.which", lambda name, path=None: None)
    monkeypatch.setattr("orchestra.node_env.find_nvm_node", lambda: b)
    monkeypatch.setenv("PATH", "/usr/bin")
    assert ensure_node() == str(b)


def test_ensure_node_prepends_path(monkeypatch, tmp_path: Path):
    root = make_nvm(tmp_path, {"v22.23.5": True})
    monkeypatch.setattr("orchestra.node_env.current_node_major", lambda path=None: 20)
    monkeypatch.setattr("orchestra.node_env.find_nvm_node", lambda: root / "versions/node/v22.23.5/bin")
    monkeypatch.setenv("PATH", "/usr/bin")
    assert ensure_node().endswith("v22.23.5/bin")
    assert os.environ["PATH"].startswith(str(root / "versions/node/v22.23.5/bin"))


WORKER = (
    "import re, sys\n"
    "tid = re.search(r'Task `(\\w+)`', sys.argv[1]).group(1)\n"
    "open(f'{tid}.py', 'w').write(f\"VALUE = '{tid}'\\n\")\n"
    "open(f'test_{tid}.py', 'w').write(f\"from {tid} import VALUE\\n\\ndef test_{tid}():\\n    assert VALUE == '{tid}'\\n\")\n"
    "print('{\"type\":\"result\",\"subtype\":\"success\",\"usage\":{\"inputTokens\":5,\"outputTokens\":2},\"finalText\":\"ok\"}')\n"
)


def tdict(tid, deps=()):
    return {"id": tid, "description": f"zrób {tid}", "files": [f"{tid}.py"], "acceptance": [f"{tid} działa"],
            "test_cmd": f"{sys.executable} -m pytest -q test_{tid}.py", "depends_on": list(deps)}


async def test_mcp_run_plan_and_status(git_repo: Path, tmp_path: Path):
    script = tmp_path / "worker.py"
    script.write_text(WORKER)
    cfg = Config(agents={"cheap": AgentConfig(name="cheap", headless=(sys.executable, str(script), "{task}"))},
                 roles={"implementer": "cheap"})
    server = build_server(config=cfg, repo=git_repo)
    async with Client(server) as client:
        started = (await client.call_tool("run_plan", {"goal": "dwa pliki", "tasks": [tdict("a"), tdict("b", ["a"])]})).data
        assert started["order"] == ["a", "b"]
        status = (await client.call_tool("run_status", {"run_id": started["run_id"], "wait_s": 60})).data
    assert status["finished"] and status["ok"], status
    assert {t["id"]: t["status"] for t in status["tasks"]} == {"a": "done", "b": "done"}
    assert status["report"]["ok"] and status["integration_branch"].startswith("orchestra/run-")
    assert all(t["tokens_in"] == 5 and t["attempts"] == 1 for t in status["tasks"])


async def test_mcp_run_plan_rejects_cycle(git_repo: Path):
    server = build_server(config=Config(agents={}, roles={}), repo=git_repo)
    async with Client(server) as client:
        try:
            await client.call_tool("run_plan", {"goal": "x", "tasks": [tdict("a", ["b"]), tdict("b", ["a"])]})
        except Exception as exc:  # noqa: BLE001
            assert "cykl" in str(exc)
        else:
            raise AssertionError("cykl powinien zostać odrzucony")


def seed(store: EventStore) -> None:
    store.add_event("scheduler", "run_start", {"run_id": "r1", "goal": "cel", "tasks": 2, "parallel": 2,
                    "integration_branch": "orchestra/run-r1",
                    "plan": [{"id": "a", "difficulty": "easy", "depends_on": [], "description": "A"},
                             {"id": "b", "difficulty": "hard", "depends_on": ["a"], "description": "B"}]})
    store.add_event("scheduler", "task_start", {"run_id": "r1", "task_id": "a"})
    store.add_event("worker:cheap", "task_attempt", {"run_id": "r1", "task_id": "a", "attempt": 1, "status": "ok"},
                    tokens_in=10, tokens_out=3)
    store.add_event("scheduler", "task_end", {"run_id": "r1", "task_id": "a", "status": "done"})


def test_build_runs_states():
    store = EventStore(":memory:")
    seed(store)
    run = build_runs(store.events())["r1"]
    assert not run.finished and run.integration_branch == "orchestra/run-r1"
    a, b = run.tasks["a"], run.tasks["b"]
    assert (a.status, a.attempts, a.worker, a.tokens_in) == ("done", 1, "cheap", 10)
    assert (b.status, b.depends_on) == ("pending", ["a"])
    store.add_event("scheduler", "run_end", {"run_id": "r1", "ok": True})
    assert build_runs(store.events())["r1"].finished


def test_fmt_event_skips_agent_stream():
    store = EventStore(":memory:")
    store.add_event("claude", "agent_message", {"text": "x"})
    seed(store)
    lines = [fmt_event(e) for e in store.events()]
    assert lines[0] is None and all(l is not None for l in lines[1:])


async def test_watch_app_renders_run():
    store = EventStore(":memory:")
    seed(store)
    app = WatchApp(store, poll_s=0.05)
    async with app.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.3)
        assert app._table.row_count == 2
        assert "run r1" in str(app._head.render())


# --- podgląd na żywo, cwd, wskazówki i anulowanie ---
import asyncio
import json
import subprocess

from orchestra.commandcode import summarize_frame
from orchestra.delegate import delegate
from orchestra.plan import Plan
from orchestra.scheduler import RunOptions, Scheduler


def frame(**ev) -> str:
    return json.dumps({"type": "event", "event": ev})


def test_summarize_frame_real_shapes():
    assert summarize_frame(frame(type="tool_queued", toolName="shell_command", input={"command": "ls"})) == \
        ("tool", "shell_command: ls")
    assert summarize_frame(frame(type="tool_queued", toolName="write_file", input={"file_path": "/x/a.py", "content": "c"})) == \
        ("tool", "write_file: /x/a.py")
    assert summarize_frame(frame(type="tool_completed", toolName="x", result=[{"type": "text", "text": "(no output)"}])) == \
        ("result", "(no output)")
    assert summarize_frame(frame(type="message_end", content=[{"type": "text", "text": "gotowe"}])) == ("text", "gotowe")
    assert summarize_frame(frame(type="thinking_end", text="hmm")) == ("think", "hmm")
    assert summarize_frame(frame(type="thinking_delta", delta="x")) is None
    assert summarize_frame("nie json") is None and summarize_frame('{"type":"result"}') is None


def _commit_dir(repo: Path, sub: str) -> None:
    (repo / sub).mkdir()
    (repo / sub / ".keep").write_text("")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "sub"], cwd=repo, check=True)


async def test_delegate_cwd_runs_worker_and_tests_in_subdir(git_repo: Path):
    _commit_dir(git_repo, "code")
    cfg = Config(agents={}, roles={})
    files = {"code/m.py": "V = 1\n", "code/test_m.py": "from m import V\n\ndef test_v():\n    assert V == 1\n"}
    res = await delegate(cfg, git_repo, "x", worker="fake", fake_files=files, cwd="code",
                         test_cmd=f"{sys.executable} -m pytest -q test_m.py", keep_worktree=False)
    assert res.status == "ok", res
    bad = await delegate(cfg, git_repo, "x", worker="fake", cwd="nie-ma", keep_worktree=False)
    assert bad.status == "worker_failed" and "cwd" in bad.error


STREAMER = (
    "import json, sys\n"
    "def f(**e): print(json.dumps({'type': 'event', 'event': e}), flush=True)\n"
    "f(type='tool_queued', toolName='write_file', input={'file_path': 'a.py'})\n"
    "open('a.py', 'w').write('V = 1\\n')\n"
    "open('test_a.py', 'w').write('from a import V\\n\\ndef test_a():\\n    assert V == 1\\n')\n"
    "f(type='tool_completed', toolName='write_file', result=[{'type': 'text', 'text': 'created'}])\n"
    "print(json.dumps({'type': 'result', 'subtype': 'success', 'usage': {'inputTokens': 1, 'outputTokens': 1}, 'finalText': 'ok'}))\n"
)


def streaming_cfg(tmp_path: Path) -> Config:
    script = tmp_path / "w.py"
    script.write_text(STREAMER)
    return Config(agents={"cheap": AgentConfig(name="cheap", headless=(sys.executable, str(script), "{task}"))},
                  roles={"implementer": "cheap"})


def one_task(tid="a", test="test_a.py"):
    return Plan.model_validate({"goal": "g", "tasks": [{
        "id": tid, "description": "d", "acceptance": ["x"], "test_cmd": f"{sys.executable} -m pytest -q {test}"}]})


async def test_scheduler_logs_live_steps_and_attempt_details(git_repo: Path, tmp_path: Path):
    store = EventStore(":memory:")
    report = await Scheduler(streaming_cfg(tmp_path), git_repo, store, RunOptions(escalate=False)).run(one_task())
    assert report.ok
    steps = store.events(agent="task:a", kind="worker_step")
    assert [(e.payload["step"], e.payload["text"]) for e in steps] == [("tool", "write_file: a.py"), ("result", "created")]
    end = store.events(agent="task:a", kind="worker_end")[0]
    assert end.payload["status"] == "ok"
    att = store.events(kind="task_attempt")[0].payload
    assert att["files"] == ["a.py", "test_a.py"] and att["test_exit_code"] == 0


async def test_failed_attempt_logs_reason(git_repo: Path, tmp_path: Path):
    store = EventStore(":memory:")
    plan = one_task(test="test_nie_ma.py")
    await Scheduler(streaming_cfg(tmp_path), git_repo, store, RunOptions(escalate=False, max_failures=1)).run(plan)
    att = store.events(kind="task_attempt")[0].payload
    assert att["status"] == "tests_failed" and "test_nie_ma.py" in att["test_output"]
    line = fmt_event(store.events(kind="task_attempt")[0])
    assert "test_nie_ma.py" in line.plain and "/detail a" in line.plain


async def test_hint_reaches_next_attempt_prompt(git_repo: Path, tmp_path: Path):
    store = EventStore(":memory:")
    prompts: list[str] = []

    async def ex(task, prompt, worker, base):
        prompts.append(prompt)
        return await delegate(Config(agents={}, roles={}), git_repo, prompt, worker="fake", base=base, keep_worktree=True)

    sched = Scheduler(Config(agents={}, roles={"implementer": "cheap"}), git_repo, store,
                      RunOptions(escalate=False, max_failures=1), executor=ex)
    store.post_message("tui", f"task:{sched.run_id}:a", "użyj funkcji foo")
    await sched.run(one_task())
    assert "użyj funkcji foo" in prompts[0]
    assert store.events(kind="hint_applied")


async def test_cancel_kills_running_task(git_repo: Path):
    store = EventStore(":memory:")
    started = asyncio.Event()

    async def slow(task, prompt, worker, base):
        started.set()
        await asyncio.sleep(60)

    sched = Scheduler(Config(agents={}, roles={"implementer": "cheap"}), git_repo, store,
                      RunOptions(escalate=False), executor=slow)
    run = asyncio.create_task(sched.run(one_task()))
    await started.wait()
    store.post_message("tui", f"run:{sched.run_id}", "cancel")
    report = await asyncio.wait_for(run, 15)
    assert report.outcomes["a"].status == "skipped" and not report.ok
    assert store.events(kind="run_cancel")


async def test_tui_commands(git_repo: Path):
    store = EventStore(":memory:")
    seed(store)  # run r1, zadania a (done) i b (pending)
    store.add_event("worker:cheap", "task_attempt",
                    {"run_id": "r1", "task_id": "b", "attempt": 1, "status": "tests_failed", "test_cmd": "pytest x",
                     "cwd": "code", "error": "", "test_output": "E assert 1 == 2", "files": ["b.py"]})
    app = WatchApp  # noqa: F841 - tylko import
    from orchestra.tui import OrchestraApp

    tui = OrchestraApp(store, poll_s=0.05)
    async with tui.run_test(size=(140, 40)) as pilot:
        await pilot.pause(0.2)
        tui.run_command("/hint b sprawdź import")
        tui.run_command("/detail b")
        tui.run_command("/cancel")
        await pilot.pause(0.1)
        assert store.inbox("task:r1:b")[0].body == "sprawdź import"
        assert store.inbox("run:r1")[0].body == "cancel"
        assert "E assert 1 == 2" in "\n".join(l.text for l in tui._log.lines)
