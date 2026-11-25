"""Child process environment, event log masking and database file permissions."""

import json
import os
import shlex
import stat
import sys
from pathlib import Path

from orchestra.commandcode import summarize_frame
from orchestra.config import AgentConfig, Config, load_config
from orchestra.delegate import delegate
from orchestra.store import EventStore
from orchestra.worktree import safe_env

ROOT = Path(__file__).resolve().parents[1]
DUMP = "import os,sys; open(sys.argv[1],'w').write('\\n'.join(sorted(os.environ)))"


def test_safe_env_drops_secrets_and_keeps_basics(monkeypatch):
    monkeypatch.setenv("OTHER_SECRET", "something-else-123456")
    monkeypatch.setenv("AWS_ACCESS_KEY_ID", "abc")
    monkeypatch.setenv("LC_ALL", "C")
    monkeypatch.setenv("ALLOWED_ONE", "1")
    env = safe_env(("ALLOWED_ONE", "NOT_SET_ANYWHERE"))
    assert env["PATH"] == os.environ["PATH"] and env["LC_ALL"] == "C" and env["ALLOWED_ONE"] == "1"
    assert not {"OTHER_SECRET", "AWS_ACCESS_KEY_ID", "NOT_SET_ANYWHERE"} & env.keys()


async def test_worker_and_test_cmd_get_trimmed_env(git_repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("OTHER_SECRET", "x" * 20)
    monkeypatch.setenv("WORKER_KEY", "y" * 20)
    monkeypatch.setenv("TEST_ONLY", "z")
    worker_dump, test_dump = tmp_path / "worker", tmp_path / "test"
    agent = AgentConfig(name="w", headless=(sys.executable, "-c", DUMP, str(worker_dump), "{task}"),
                        env_passthrough=("WORKER_KEY",))
    cfg = Config(agents={"w": agent}, roles={}, test_env_passthrough=("TEST_ONLY",))
    res = await delegate(cfg, git_repo, "x", worker="w",
                         test_cmd=shlex.join([sys.executable, "-c", DUMP, str(test_dump)]))
    assert res.status == "ok", res
    worker, test = worker_dump.read_text().split("\n"), test_dump.read_text().split("\n")
    assert "WORKER_KEY" in worker and "OTHER_SECRET" not in worker and "TEST_ONLY" not in worker
    assert "TEST_ONLY" in test and "OTHER_SECRET" not in test and "WORKER_KEY" not in test


def test_load_config_reads_test_env_passthrough(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("ORCHESTRA_USER_CONFIG", str(tmp_path / "none.toml"))
    path = tmp_path / "agents.toml"
    path.write_text('test_env_passthrough = ["DATABASE_URL"]\n')
    assert load_config(path).test_env_passthrough == ("DATABASE_URL",)


def test_event_payload_is_masked(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MY_API_KEY", "supersecretvalue123")
    store = EventStore(tmp_path / "e.db")
    store.add_event("a", "worker_step", {"run_id": "r1", "n": 3, "text": "key supersecretvalue123",
                                         "nested": [{"out": "ghp_ABCDEFGHIJKLMNOPQRSTUV"}]})
    payload = store.events()[0].payload
    assert "supersecretvalue123" not in json.dumps(payload) and "ghp_ABC" not in json.dumps(payload)
    assert payload["run_id"] == "r1" and payload["n"] == 3


def test_truncated_frame_does_not_leak_secret_prefix(monkeypatch):
    secret = "supersecretvalue123"
    monkeypatch.setenv("MY_API_KEY", secret)
    frame = {"type": "event", "event": {"type": "message_end", "content": [{"type": "text", "text": "a" * 290 + secret}]}}
    frame_text = {"type": "event", "event": {"type": "thinking_end", "text": "a" * 190 + secret}}
    for f in (frame, frame_text):
        step = summarize_frame(json.dumps(f))
        assert step is not None and "supersec" not in step[1]


def test_db_files_are_private(tmp_path: Path):
    path = tmp_path / "e.db"
    path.touch(mode=0o644)
    path.chmod(0o644)
    store = EventStore(path)
    store.add_event("a", "k", {})
    files = [p for p in tmp_path.iterdir() if p.name.startswith("e.db")]
    assert files and all(stat.S_IMODE(p.stat().st_mode) == 0o600 for p in files), files
