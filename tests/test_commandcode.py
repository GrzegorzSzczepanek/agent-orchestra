import json
import sys
from pathlib import Path

import pytest

from orchestra.commandcode import (
    mask_secrets,
    parse_headless_output,
    parse_models,
    resolve_model,
    status_for_exit_code,
)
from orchestra.config import AgentConfig, Config
from orchestra.delegate import delegate

FIX = Path(__file__).parent / "fixtures"


def test_parse_real_unauth_result_line():
    # prawdziwa linia z `cmd -p ... --output-format json` bez logowania (cmd 1.93.2)
    r = parse_headless_output((FIX / "cmd_result_unauth.ndjson").read_text())
    assert r.found_result and r.subtype == "error"
    assert "cmd login" in r.error
    assert r.usage == {"tokens_in": 0, "tokens_out": 0, "cache_read": 0, "cache_write": 0}


def test_parse_success_with_events_unknown_frames_and_garbage():
    out = "\n".join([
        "log nie json",
        json.dumps({"type": "event", "event": {"type": "tool_running", "toolCallId": "1", "toolName": "read_file"}}),
        json.dumps({"type": "event", "event": {"type": "cos_nowego", "x": 1}}),
        '{"zepsute": ',
        json.dumps({"type": "future_frame"}),
        json.dumps({"type": "result", "subtype": "success", "sessionId": "s1", "stopReason": "end_turn",
                    "usage": {"inputTokens": 100, "outputTokens": 20, "cacheReadTokens": 80, "cacheWriteTokens": 5},
                    "durationMs": 12, "finalText": "gotowe"}),
    ])
    r = parse_headless_output(out)
    assert (r.subtype, r.final_text, r.session_id, r.stop_reason) == ("success", "gotowe", "s1", "end_turn")
    assert r.events == 2 and r.parse_errors == 1
    assert r.usage["tokens_in"] == 100 and r.usage["cache_read"] == 80


def test_parse_optional_fields_missing_and_claude_format():
    r = parse_headless_output(json.dumps({"type": "result", "subtype": "max_turns", "usage": {}, "finalText": ""}))
    assert r.subtype == "max_turns" and r.session_id == "" and r.stop_reason == ""
    claude = {"type": "result", "subtype": "success", "is_error": False, "result": "ok",
              "session_id": "c1", "total_cost_usd": 0.0123, "usage": {"input_tokens": 7, "output_tokens": 3}}
    r = parse_headless_output(json.dumps(claude))
    assert r.final_text == "ok" and r.cost_usd == pytest.approx(0.0123) and r.usage["tokens_out"] == 3


def test_parse_no_result_line():
    r = parse_headless_output("tylko tekst\n")
    assert not r.found_result and r.subtype == ""


def test_parse_real_claude_usage_limit_is_error_despite_success_subtype():
    """Prawdziwa odpowiedź `claude -p --output-format json` przy limicie: subtype=success, is_error=true."""
    r = parse_headless_output((FIX / "claude_result_usage_limit.json").read_text())
    assert r.found_result and r.subtype == "success"
    assert r.is_error and "monthly spend limit" in r.error
    assert r.usage["tokens_in"] == 0 and r.cost_usd == 0.0


def test_parse_real_claude_success_fixture():
    r = parse_headless_output((FIX / "claude_result_success.json").read_text())
    assert r.found_result and not r.is_error and r.final_text == "ok"
    assert r.usage == {"tokens_in": 15304, "tokens_out": 37, "cache_read": 0, "cache_write": 0}
    assert r.cost_usd == pytest.approx(0.0123)


def test_exit_code_mapping():
    assert status_for_exit_code(0) == "ok"
    assert status_for_exit_code(8) == "max_turns"
    assert status_for_exit_code(10) == "insufficient_credits"
    assert status_for_exit_code(3) == "auth_failed"
    assert status_for_exit_code(99) == "worker_failed"


def test_parse_models_real_output():
    models = parse_models((FIX / "cmd_list_models.txt").read_text())
    ids = {m.id for m in models}
    assert len(models) == 105  # tyle podaje nagłówek `Available models · 105 models`
    assert "typesafe/jev" not in {m.id for m in models}
    assert {"deepseek/deepseek-v4.1-flash", "z-ai/glm-5.3-flash", "moonshotai/kimi-k2.5"} <= ids
    # linie pomocnicze nie są modelami
    assert not any(i.startswith(("cmd", "Docs", "Pass")) for i in ids)
    assert all("://" not in i and not i.endswith(":") for i in ids)
    assert any(m.id == "recursive/flint-v1.0:free" for m in models)


def test_resolve_model_full_short_and_case():
    models = parse_models((FIX / "cmd_list_models.txt").read_text())
    assert resolve_model("deepseek/deepseek-v4.1-flash", models) == "deepseek/deepseek-v4.1-flash"
    assert resolve_model("GLM-5.3-FLASH", models) == "z-ai/glm-5.3-flash"
    assert resolve_model("kimi-k2.5", models) == "moonshotai/kimi-k2.5"
    assert resolve_model("nie-ma-takiego", models) is None
    assert resolve_model("", models) is None


def test_mask_secrets():
    env = {"COMMAND_CODE_API_KEY": "abcd1234efgh5678", "HOME": "/home/x"}
    text = ("klucz abcd1234efgh5678 oraz Bearer eyJhbGciOiJIUzI1NiJ9.xxxxxxxx.yyyy "
            "i token=ghp_ABCDEFGHIJKLMNOPQRSTUV oraz sk-ant-api03-ABCDEFGHIJKL oraz /home/x")
    out = mask_secrets(text, env)
    assert "abcd1234efgh5678" not in out and "eyJhbGci" not in out
    assert "ghp_ABCDEF" not in out and "sk-ant-api03" not in out
    assert "/home/x" in out


def _script(tmp_path: Path, body: str) -> AgentConfig:
    f = tmp_path / "w.py"
    f.write_text(body)
    return AgentConfig(name="w", headless=(sys.executable, str(f), "{task}"))


@pytest.mark.parametrize("subtype,code,expected", [
    ("max_turns", 8, "max_turns"),
    ("error", 10, "insufficient_credits"),
    ("error", 3, "auth_failed"),
    ("max_turns", 0, "max_turns"),  # sam subtype wystarcza
    ("error", 0, "worker_failed"),
])
async def test_delegate_statuses(git_repo: Path, tmp_path: Path, subtype, code, expected):
    line = {"type": "result", "subtype": subtype, "usage": {}, "durationMs": 1, "finalText": "", "error": "boom"}
    agent = _script(tmp_path, f"import json, sys\nprint(json.dumps({line!r}))\nsys.exit({code})\n")
    res = await delegate(Config(agents={"w": agent}, roles={}), git_repo, "x", worker="w",
                         test_cmd=f"{sys.executable} -c 'raise SystemExit(1)'")
    assert res.status == expected, res
    assert res.test_exit_code is None  # bramka testów nie rusza przy porażce workera
    assert res.worker_exit_code == code
    assert res.error


async def test_delegate_claude_is_error_with_zero_exit_is_worker_failed(git_repo: Path, tmp_path: Path):
    """Claude potrafi zwrócić kod 0 i subtype=success, a mimo to is_error=true (np. limit zużycia)."""
    fixture = FIX / "claude_result_usage_limit.json"
    agent = _script(tmp_path, f"import sys\nsys.stdout.write(open({str(fixture)!r}).read())\n")
    res = await delegate(Config(agents={"w": agent}, roles={}), git_repo, "x", worker="w")
    assert res.status == "worker_failed" and "monthly spend limit" in res.error
    assert res.worker_exit_code == 0


async def test_delegate_model_required_is_config_error(git_repo: Path, monkeypatch):
    monkeypatch.delenv("ORCHESTRA_CMD_MODEL", raising=False)
    agent = AgentConfig(name="w", headless=("echo", "--model", "{model}", "{task}"),
                        require_model=True, model_env="ORCHESTRA_CMD_MODEL")
    res = await delegate(Config(agents={"w": agent}, roles={}), git_repo, "x", worker="w")
    assert res.status == "config_error" and "ORCHESTRA_CMD_MODEL" in res.error


async def test_delegate_masks_secrets_in_error(git_repo: Path, tmp_path: Path, monkeypatch):
    monkeypatch.setenv("COMMAND_CODE_API_KEY", "supersecretvalue123")
    agent = _script(tmp_path, "import sys\nprint('klucz supersecretvalue123', file=sys.stderr)\nsys.exit(2)\n")
    res = await delegate(Config(agents={"w": agent}, roles={}), git_repo, "x", worker="w")
    assert res.status == "worker_failed" and "supersecretvalue123" not in res.error
