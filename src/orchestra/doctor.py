"""`orchestra doctor`: diagnostyka środowiska (cmd, logowanie, modele, headless, ACP, opcjonalnie inni agenci).

Raport PASS/FAIL/SKIP/WARN trafia na ekran i do JSON, który można wkleić do rozmowy.
Żadna wartość zmiennej środowiskowej ani token nie trafia do raportu (maskowanie w mask_secrets).
"""

from __future__ import annotations

import asyncio
import json
import platform
import re
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from acp import PROTOCOL_VERSION, spawn_agent_process
from acp.interfaces import Client

from . import __version__
from .commandcode import (
    mask_secrets,
    parse_headless_output,
    parse_models,
    resolve_model,
    status_for_exit_code,
    strip_ansi,
)
from .config import AgentConfig, Config
from .worktree import run

PASS, FAIL, SKIP, WARN = "PASS", "FAIL", "SKIP", "WARN"
CMD_AGENT = "commandcode"


@dataclass
class Check:
    name: str
    status: str
    detail: str = ""
    hint: str = ""


@dataclass
class Report:
    checks: list[Check] = field(default_factory=list)
    meta: dict[str, Any] = field(default_factory=dict)

    def add(self, name: str, status: str, detail: str = "", hint: str = "") -> Check:
        c = Check(name, status, mask_secrets(detail), mask_secrets(hint))
        self.checks.append(c)
        return c

    def summary(self) -> dict[str, int]:
        out = {PASS: 0, FAIL: 0, SKIP: 0, WARN: 0}
        for c in self.checks:
            out[c.status] = out.get(c.status, 0) + 1
        return out

    @property
    def ok(self) -> bool:
        return self.summary()[FAIL] == 0

    def to_dict(self) -> dict[str, Any]:
        return {**self.meta, "summary": self.summary(), "checks": [asdict(c) for c in self.checks]}

    def render(self) -> str:
        lines = []
        for c in self.checks:
            lines.append(f"[{c.status}] {c.name}" + (f": {c.detail}" if c.detail else ""))
            if c.hint:
                lines.append(f"       co zrobić: {c.hint}")
        s = self.summary()
        lines.append("")
        lines.append(f"Podsumowanie: {s[PASS]} PASS, {s[FAIL]} FAIL, {s[WARN]} WARN, {s[SKIP]} SKIP")
        return "\n".join(lines)

    def save(self, path: str | Path) -> None:
        Path(path).write_text(json.dumps(self.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class _NullClient(Client):
    async def session_update(self, session_id: str, update: Any, **kwargs: Any) -> None:
        return None

    async def request_permission(self, *a: Any, **k: Any) -> Any:  # nie powinno się zdarzyć przy samym initialize
        raise RuntimeError("diagnostyka nie obsługuje uprawnień")


async def probe_acp(
    argv: Sequence[str], *, cwd: str, timeout: float = 30, new_session: bool = False,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Startuje agenta ACP, robi initialize (i opcjonalnie session/new), zamyka proces.

    Zwraca {"agent": {...}, "session": "ok" | błąd}. Wyjątki propagują do wywołującego.
    """

    async def _go() -> dict[str, Any]:
        async with spawn_agent_process(_NullClient(), argv[0], *argv[1:], cwd=cwd, env=env or None) as (conn, _proc):
            init = await conn.initialize(protocol_version=PROTOCOL_VERSION)
            info = getattr(init, "agent_info", None)
            out: dict[str, Any] = {
                "agent": {"name": getattr(info, "name", ""), "version": getattr(info, "version", "")},
                "protocol_version": getattr(init, "protocol_version", None),
                "session": "pominięte",
            }
            if new_session:
                try:
                    await conn.new_session(cwd=cwd, mcp_servers=[])
                    out["session"] = "ok"
                except Exception as exc:  # noqa: BLE001 - błąd sesji (np. brak logowania) jest wynikiem diagnostyki
                    out["session"] = f"błąd: {exc}"
            return out

    return await asyncio.wait_for(_go(), timeout)


async def _capture(argv: Sequence[str], timeout: float, cwd: str | None = None) -> tuple[int, str, str]:
    code, out, err = await run(list(argv), cwd or tempfile.gettempdir(), timeout=timeout)
    return code, strip_ansi(out), strip_ansi(err)


def _first_line(text: str, limit: int = 200) -> str:
    for line in text.splitlines():
        if line.strip():
            return line.strip()[:limit]
    return ""


_VERSION_RE = re.compile(r"^v?\d+\.\d+(\.\d+)?\S{0,30}$")


def _looks_like_version(line: str) -> bool:
    """Wersja to krótki token (np. `1.93.2`), a nie surowa linia błędu czy kawałek minifikowanego kodu."""
    return bool(_VERSION_RE.match(line.split(" ")[0])) and len(line) <= 80


def _node_engines(binary: str) -> str:
    """Pole engines.node pakietu npm, do którego należy binarka (idziemy w górę od realpath)."""
    exe = shutil.which(binary)
    if not exe:
        return ""
    for parent in Path(exe).resolve().parents:
        pkg = parent / "package.json"
        if pkg.is_file():
            try:
                data = json.loads(pkg.read_text())
            except (OSError, ValueError):
                continue
            if data.get("bin") or data.get("name"):
                return str(data.get("engines", {}).get("node", ""))
    return ""


def _node_satisfies(version: str, engines: str) -> bool | None:
    """Obsługuje zakres `>=N[.M]`; dla innych zapisów zwraca None (nie rozstrzygamy)."""
    m = re.fullmatch(r"\s*>=\s*v?(\d+)(?:\.(\d+))?[\d.]*\s*", engines)
    v = re.match(r"v?(\d+)\.(\d+)", version)
    if not (m and v):
        return None
    return (int(v[1]), int(v[2])) >= (int(m[1]), int(m[2] or 0))


async def _diagnose_crash(binary: str, out: str, err: str, timeout: float) -> tuple[str, str]:
    """Zamienia crash przy starcie (stack trace z cli.mjs) w czytelny komunikat i podpowiedź."""
    engines = _node_engines(binary)
    node = ""
    try:
        _, nout, _ = await _capture(["node", "-v"], timeout)
        node = _first_line(nout)
    except Exception:  # noqa: BLE001
        pass
    if engines and node and _node_satisfies(node, engines) is False:
        need = engines.replace(">=", "").strip()
        return (f"{binary} nie startuje: wymagany Node >={need}, masz {node}",
                f"przełącz Node (np. `nvm use {need}`) albo zainstaluj pakiet pod nowszym Node")
    lines = [l.strip() for l in (err or out).splitlines() if l.strip()]
    err_line = next((l for l in reversed(lines) if re.match(r"^\w*Error\b", l)), "")
    if not err_line:
        # Node obcina stderr do ok. 64 KB przy zamknięciu potoku, więc stack trace po długiej linii cli.mjs ginie
        why = "brak pola engines w zainstalowanym pakiecie" if not engines else f"engines {engines}"
        return (f"{binary} nie startuje pod Node {node or '?'} ({why}, komunikat błędu obcięty)",
                "aktualny command-code wymaga Node >=22: `nvm use 22` albo `npm i -g command-code@latest` pod Node 22")
    return (f"{binary} nie startuje ({err_line[:160]}), Node {node or '?'}",
            "uruchom `cmd --version` ręcznie i sprawdź wersję Node względem engines pakietu")


async def _check_commandcode(
    rep: Report, agent: AgentConfig, *, smoke: bool, model: str, timeout: float, cwd: str
) -> None:
    binary = (agent.acp or agent.headless or ("cmd",))[0]
    if not shutil.which(binary):
        rep.add("cmd: binarka", FAIL, f"brak `{binary}` w PATH",
                "zainstaluj: npm i -g command-code (potem uruchom ponownie ten terminal)")
        for name in ("cmd: logowanie", "cmd: lista modeli", "cmd: smoke test headless", "cmd: ACP initialize",
                     "cmd: ACP session/new"):
            rep.add(name, SKIP, "pominięte, bo brak binarki")
        return

    try:
        code, out, err = await _capture([binary, "--version"], timeout)
        version = _first_line(out) or _first_line(err)
        crashed = code != 0 or not _looks_like_version(version)
        hint = ""
        if crashed:
            detail, hint = await _diagnose_crash(binary, out, err, timeout)
    except Exception as exc:  # noqa: BLE001
        crashed, detail, hint, version = True, f"{binary}: {type(exc).__name__}: {exc}", "", ""
    rep.meta["cmd_version"] = "" if crashed else version
    if crashed:
        rep.add("cmd: binarka", FAIL, detail, hint)
        for name in ("cmd: logowanie", "cmd: lista modeli", "cmd: smoke test headless", "cmd: ACP initialize",
                     "cmd: ACP session/new"):
            rep.add(name, SKIP, "pominięte, bo cmd nie startuje")
        return
    rep.add("cmd: binarka", PASS, f"{binary} {version}")

    # logowanie (zmienna COMMAND_CODE_API_KEY ma pierwszeństwo przed `cmd login`)
    try:
        code, out, err = await _capture([binary, "status"], timeout)
        text = (out + err)
        if code == 0:
            rep.add("cmd: logowanie", PASS, _first_line(text.replace("- Checking authentication status...", "")) or "zalogowany")
        else:
            rep.add("cmd: logowanie", FAIL, "niezalogowany",
                    "uruchom ręcznie `cmd login` (albo ustaw COMMAND_CODE_API_KEY, np. na serwerze bez przeglądarki)")
    except Exception as exc:  # noqa: BLE001
        rep.add("cmd: logowanie", FAIL, f"{type(exc).__name__}: {exc}")

    # lista modeli i wybrany model
    models = []
    try:
        code, out, err = await _capture([binary, "--list-models"], timeout)
        models = parse_models(out)
        if code == 0 and models:
            rep.add("cmd: lista modeli", PASS, f"{len(models)} modeli")
        else:
            rep.add("cmd: lista modeli", FAIL, _first_line(err) or "pusta lista", "zaktualizuj: cmd update")
    except Exception as exc:  # noqa: BLE001
        rep.add("cmd: lista modeli", FAIL, f"{type(exc).__name__}: {exc}")
    chosen = agent.resolve_model(model or None)
    if not chosen:
        rep.add("cmd: wybrany model", WARN, "nie ustawiono modelu dla workera",
                f"ustaw {agent.model_env or 'default_model'} na ID z `cmd --list-models`, np. deepseek/deepseek-v4.1-flash")
    elif models:
        resolved = resolve_model(chosen, models)
        if resolved:
            rep.add("cmd: wybrany model", PASS, f"{chosen} -> {resolved}")
        else:
            rep.add("cmd: wybrany model", FAIL, f"`{chosen}` nie ma na liście modeli",
                    "wybierz ID z `cmd --list-models` (pełne albo część po ostatnim '/')")
    else:
        rep.add("cmd: wybrany model", SKIP, f"{chosen} (bez listy modeli nie da się zweryfikować)")

    # smoke test headless (zużywa kredyty, więc tylko na życzenie)
    if not smoke:
        rep.add("cmd: smoke test headless", SKIP, "pominięty (zużywa niewielką ilość kredytów)",
                "uruchom `orchestra doctor --smoke`; dokumentacja nie mówi, czy plan Go obejmuje tryb headless")
    else:
        argv = [binary, "-p", "odpowiedz: ok", "--max-turns", "1", "--output-format", "json", "--skip-onboarding"]
        if chosen:
            argv += ["--model", chosen]
        try:
            code, out, err = await _capture(argv, max(timeout, 120), cwd=cwd)
            hr = parse_headless_output(out)
            if code == 0 and hr.subtype == "success":
                rep.add("cmd: smoke test headless", PASS,
                        f"odpowiedź: {hr.final_text[:60]!r}, tokeny wej/wyj: {hr.usage.get('tokens_in', 0)}/{hr.usage.get('tokens_out', 0)}")
            else:
                status = status_for_exit_code(code)
                hint = {
                    "auth_failed": "zaloguj się: cmd login",
                    "insufficient_credits": "brak kredytów lub limit planu (Go: 3 USD/5 h, 6 USD/7 dni, 10 USD/mies.)",
                    "rate_limited": "limit zapytań, spróbuj za chwilę",
                    "max_turns": "limit tur osiągnięty (to też oznacza, że headless działa)",
                }.get(status, "wklej ten raport, dopasuję parser")
                st = WARN if status == "max_turns" else FAIL
                rep.add("cmd: smoke test headless", st,
                        f"{status} (kod {code}, subtype={hr.subtype or '-'}): {hr.error or _first_line(err)}", hint)
        except Exception as exc:  # noqa: BLE001
            rep.add("cmd: smoke test headless", FAIL, f"{type(exc).__name__}: {exc}")

    # ACP
    argv = list(agent.acp) or [binary, "acp"]
    try:
        info = await probe_acp(argv, cwd=cwd, timeout=timeout, new_session=True, env=agent.acp_env())
        a = info["agent"]
        rep.add("cmd: ACP initialize", PASS, f"{a['name']} {a['version']}, protokół v{info['protocol_version']}")
        if info["session"] == "ok":
            rep.add("cmd: ACP session/new", PASS)
        else:
            rep.add("cmd: ACP session/new", FAIL, info["session"], "uruchom ręcznie `cmd login` (auth_required)")
    except Exception as exc:  # noqa: BLE001 - także asyncio.TimeoutError
        rep.add("cmd: ACP initialize", FAIL, f"{type(exc).__name__}: {exc}",
                "sprawdź `cmd acp` ręcznie; w `settings` mod `acp` nie może być wyłączony")
        rep.add("cmd: ACP session/new", SKIP, "pominięte")


async def _check_optional_agent(rep: Report, name: str, agent: AgentConfig, *, timeout: float, cwd: str) -> None:
    headless_bin = agent.headless[0] if agent.headless else ""
    acp_bin = agent.acp[0] if agent.acp else ""
    if headless_bin:
        found = shutil.which(headless_bin)
        rep.add(f"{name}: binarka", PASS if found else SKIP, headless_bin if found else f"brak `{headless_bin}` w PATH")
    if not acp_bin:
        return
    if not shutil.which(acp_bin):
        rep.add(f"{name}: ACP initialize", SKIP, f"brak `{acp_bin}` w PATH")
        return
    try:
        info = await probe_acp(list(agent.acp), cwd=cwd, timeout=timeout, new_session=False, env=agent.acp_env())
        a = info["agent"]
        rep.add(f"{name}: ACP initialize", PASS, f"{a['name']} {a['version']}".strip() or "ok")
    except Exception as exc:  # noqa: BLE001
        rep.add(f"{name}: ACP initialize", FAIL, f"{type(exc).__name__}: {exc}",
                "sprawdź komendę `acp` w agents.toml i wersję agenta")


async def run_doctor(
    config: Config,
    *,
    smoke: bool = False,
    model: str = "",
    timeout: float = 30,
    others: bool = True,
) -> Report:
    rep = Report(meta={
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "orchestra_version": __version__,
        "python": sys.version.split()[0],
        "platform": f"{platform.system()} {platform.release()}",
    })
    with tempfile.TemporaryDirectory(prefix="orchestra-doctor-") as cwd:
        agent = config.agents.get(CMD_AGENT)
        if agent is None:
            rep.add("cmd: konfiguracja", FAIL, f"brak agenta `{CMD_AGENT}` w agents.toml")
        else:
            await _check_commandcode(rep, agent, smoke=smoke, model=model, timeout=timeout, cwd=cwd)
        if others:
            for name, ag in config.agents.items():
                if name != CMD_AGENT:
                    await _check_optional_agent(rep, name, ag, timeout=timeout, cwd=cwd)
    return rep
