import json
import os
import stat
import sys
from pathlib import Path

import pytest

from orchestra.config import load_config
from orchestra.doctor import FAIL, PASS, SKIP, WARN, run_doctor

ROOT = Path(__file__).resolve().parent.parent
FIXTURE_MODELS = Path(__file__).parent / "fixtures" / "cmd_list_models.txt"

FAKE_CMD = f"""#!{sys.executable}
import json, os, sys
a = sys.argv[1:]
if a[:1] == ["--version"]:
    print("1.93.2"); sys.exit(0)
if a[:1] == ["status"]:
    if os.environ.get("FAKE_CMD_UNAUTH"):
        print("× Not authenticated"); sys.exit(1)
    print("Logged in as test@example.com\\nSource: $COMMAND_CODE_API_KEY"); sys.exit(0)
if a[:1] == ["--list-models"]:
    sys.stdout.write(open({str(FIXTURE_MODELS)!r}).read()); sys.exit(0)
if a[:1] == ["acp"]:
    if os.environ.get("FAKE_CMD_ACP") == "die":
        sys.exit(1)
    if os.environ.get("FAKE_CMD_DUMP"):
        open(os.environ["FAKE_CMD_DUMP"], "w").write(json.dumps(
            {{"has_key": bool(os.environ.get("COMMAND_CODE_API_KEY")), "has_other": bool(os.environ.get("OTHER_SECRET"))}}))
    os.execv({sys.executable!r}, [{sys.executable!r}, "-m", "orchestra.testing.mock_agent"])
if "-p" in a:
    code = int(os.environ.get("FAKE_CMD_HEADLESS_CODE", "0"))
    sub = {{0: "success", 8: "max_turns"}}.get(code, "error")
    print(json.dumps({{"type": "event", "event": {{"type": "tool_running"}}}}))
    print(json.dumps({{"type": "result", "subtype": sub, "usage": {{"inputTokens": 12, "outputTokens": 3,
        "cacheReadTokens": 0, "cacheWriteTokens": 0}}, "durationMs": 5, "finalText": "ok",
        "error": "brak kredytow" if sub == "error" else None}}))
    sys.exit(code)
sys.exit(2)
"""


@pytest.fixture
def fake_path(tmp_path: Path, monkeypatch):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    cmd = bin_dir / "cmd"
    cmd.write_text(FAKE_CMD)
    cmd.chmod(cmd.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", str(bin_dir))  # nic poza fałszywym cmd: reszta agentów ma dostać SKIP
    monkeypatch.setenv("ORCHESTRA_CMD_MODEL", "glm-5.3-flash")
    for var in ("FAKE_CMD_UNAUTH", "FAKE_CMD_ACP", "FAKE_CMD_HEADLESS_CODE"):
        monkeypatch.delenv(var, raising=False)
    return bin_dir


def by_name(rep):
    return {c.name: c for c in rep.checks}


async def test_doctor_all_pass_with_fake_cmd(fake_path, monkeypatch):
    cfg = load_config(ROOT / "agents.toml")
    rep = await run_doctor(cfg, smoke=True, timeout=20)
    c = by_name(rep)
    assert c["cmd: binarka"].status == PASS and "1.93.2" in c["cmd: binarka"].detail
    assert c["cmd: logowanie"].status == PASS
    assert c["cmd: lista modeli"].status == PASS and "105" in c["cmd: lista modeli"].detail
    assert c["cmd: wybrany model"].status == PASS and "z-ai/glm-5.3-flash" in c["cmd: wybrany model"].detail
    assert c["cmd: smoke test headless"].status == PASS and "12/3" in c["cmd: smoke test headless"].detail
    assert c["cmd: ACP initialize"].status == PASS
    assert c["cmd: ACP session/new"].status == PASS
    # pozostali agenci nie są w PATH
    assert c["codex: ACP initialize"].status == SKIP and c["antigravity: binarka"].status == SKIP
    assert rep.ok


async def test_doctor_smoke_skipped_by_default(fake_path):
    rep = await run_doctor(load_config(ROOT / "agents.toml"), timeout=20, others=False)
    c = by_name(rep)
    assert c["cmd: smoke test headless"].status == SKIP and "--smoke" in c["cmd: smoke test headless"].hint


async def test_doctor_unauthenticated_and_bad_model(fake_path, monkeypatch):
    monkeypatch.setenv("FAKE_CMD_UNAUTH", "1")
    monkeypatch.setenv("ORCHESTRA_CMD_MODEL", "model-ktorego-nie-ma")
    rep = await run_doctor(load_config(ROOT / "agents.toml"), timeout=20, others=False)
    c = by_name(rep)
    assert c["cmd: logowanie"].status == FAIL and "cmd login" in c["cmd: logowanie"].hint
    assert c["cmd: wybrany model"].status == FAIL
    assert not rep.ok


async def test_doctor_model_not_set_is_warning(fake_path, monkeypatch):
    monkeypatch.delenv("ORCHESTRA_CMD_MODEL")
    rep = await run_doctor(load_config(ROOT / "agents.toml"), timeout=20, others=False)
    c = by_name(rep)
    assert c["cmd: wybrany model"].status == WARN and "ORCHESTRA_CMD_MODEL" in c["cmd: wybrany model"].hint


@pytest.mark.parametrize("code,status,needle", [(10, FAIL, "insufficient_credits"), (8, WARN, "max_turns"),
                                                (3, FAIL, "auth_failed")])
async def test_doctor_smoke_error_codes(fake_path, monkeypatch, code, status, needle):
    monkeypatch.setenv("FAKE_CMD_HEADLESS_CODE", str(code))
    rep = await run_doctor(load_config(ROOT / "agents.toml"), smoke=True, timeout=20, others=False)
    c = by_name(rep)["cmd: smoke test headless"]
    assert c.status == status and needle in c.detail


def _with_extra_passthrough(*names: str):
    import dataclasses

    cfg = load_config(ROOT / "agents.toml")
    ag = cfg.agents["commandcode"]
    agents = {**cfg.agents, "commandcode": dataclasses.replace(ag, env_passthrough=ag.env_passthrough + names)}
    return dataclasses.replace(cfg, agents=agents)


async def test_doctor_acp_failure_is_reported_not_raised(fake_path, monkeypatch):
    monkeypatch.setenv("FAKE_CMD_ACP", "die")
    rep = await run_doctor(_with_extra_passthrough("FAKE_CMD_ACP"), timeout=5, others=False)
    c = by_name(rep)
    assert c["cmd: ACP initialize"].status == FAIL and c["cmd: ACP session/new"].status == SKIP


async def test_doctor_missing_cmd(tmp_path, monkeypatch):
    monkeypatch.setenv("PATH", str(tmp_path))  # pusty PATH
    rep = await run_doctor(load_config(ROOT / "agents.toml"), timeout=5)
    c = by_name(rep)
    assert c["cmd: binarka"].status == FAIL and "npm i -g command-code" in c["cmd: binarka"].hint
    assert c["cmd: ACP initialize"].status == SKIP


async def test_doctor_never_leaks_secrets(fake_path, monkeypatch, tmp_path):
    secret = "tajnyklucz-1234567890"
    monkeypatch.setenv("COMMAND_CODE_API_KEY", secret)
    monkeypatch.setenv("FAKE_CMD_UNAUTH", "1")
    rep = await run_doctor(load_config(ROOT / "agents.toml"), smoke=True, timeout=20)
    rep.add("test", PASS, f"wyciek {secret} Bearer abcdefghijklmnop12345")
    out = tmp_path / "r.json"
    rep.save(out)
    blob = out.read_text() + rep.render()
    assert secret not in blob and "abcdefghijklmnop12345" not in blob
    data = json.loads(out.read_text())
    assert data["summary"] and data["checks"] and data["cmd_version"] == "1.93.2"
    assert "environ" not in blob.lower()


def test_cli_doctor_writes_json(fake_path, tmp_path, monkeypatch, capsys):
    from orchestra.cli import main

    out = tmp_path / "rep.json"
    monkeypatch.chdir(ROOT)
    rc = main(["doctor", "--json", str(out), "--no-others", "--timeout", "20"])
    assert rc == 0 and out.exists()
    assert "Podsumowanie" in capsys.readouterr().out


async def test_acp_child_gets_only_passthrough_env(fake_path, monkeypatch, tmp_path):
    """SDK ACP daje agentowi okrojone środowisko: klucz cmd musi być przekazany jawnie, reszta sekretów nie."""
    dump = tmp_path / "dump.json"
    monkeypatch.setenv("FAKE_CMD_DUMP", str(dump))
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "klucz-testowy-123456")
    monkeypatch.setenv("OTHER_SECRET", "cos-innego-123456")
    await run_doctor(_with_extra_passthrough("FAKE_CMD_DUMP"), timeout=20, others=False)
    assert json.loads(dump.read_text()) == {"has_key": True, "has_other": False}
