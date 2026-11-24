"""Ładowanie konfiguracji agentów z agents.toml."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


class ModelRequired(ValueError):
    """Agent wymaga jawnie ustawionego modelu (ID z `cmd --list-models`), a żaden nie został podany."""


@dataclass(frozen=True)
class AgentConfig:
    name: str
    description: str = ""
    acp: tuple[str, ...] = ()
    headless: tuple[str, ...] = ()
    tier: str = "strong"
    default_model: str = ""
    model_env: str = ""  # nazwa zmiennej środowiskowej z ID modelu (np. ORCHESTRA_CMD_MODEL)
    require_model: bool = False  # gdy true, brak modelu to jasny błąd zamiast domyślnego modelu agenta
    # Zmienne środowiskowe przekazywane agentowi uruchamianemu po ACP. SDK ACP startuje proces z okrojonym
    # środowiskiem (HOME, PATH itp.), więc np. COMMAND_CODE_API_KEY trzeba przekazać jawnie.
    env_passthrough: tuple[str, ...] = ()

    def binary(self) -> str:
        """Pierwszy element komendy headless (a gdy jej brak, ACP): to, co musi być w PATH."""
        argv = self.headless or self.acp
        return argv[0] if argv else ""

    def is_available(self) -> bool:
        """Czy binarka agenta jest zainstalowana (w PATH albo jako ścieżka)."""
        binary = self.binary()
        return bool(binary) and shutil.which(binary) is not None

    def acp_env(self) -> dict[str, str]:
        """Zmienne z env_passthrough, które są ustawione w bieżącym środowisku."""
        return {k: os.environ[k] for k in self.env_passthrough if os.environ.get(k)}

    def resolve_model(self, model: str | None = None) -> str:
        """Kolejność: argument, zmienna środowiskowa model_env, pole default_model."""
        env_value = os.environ.get(self.model_env, "").strip() if self.model_env else ""
        return (model or env_value or self.default_model).strip()

    def headless_argv(self, task: str, model: str | None = None) -> list[str]:
        """Zwraca argv trybu headless z podstawionym zadaniem i modelem."""
        if not self.headless:
            raise ValueError(f"Agent {self.name} nie ma komendy headless")
        chosen = self.resolve_model(model)
        if self.require_model and not chosen and any("{model}" in part for part in self.headless):
            hint = f"zmienną {self.model_env}" if self.model_env else "pole default_model w agents.toml"
            raise ModelRequired(
                f"Agent {self.name} wymaga modelu: ustaw {hint} na ID z `cmd --list-models` "
                f"(np. deepseek/deepseek-v4.1-flash) albo podaj argument model."
            )
        out: list[str] = []
        for part in self.headless:
            if part == "{model}" and not chosen:
                # brak modelu: usuń też poprzedzającą flagę --model/-m
                if out and out[-1] in ("--model", "-m"):
                    out.pop()
                continue
            out.append(part.replace("{task}", task).replace("{model}", chosen))
        return out


ROLE_NAMES = ("planner", "reviewer", "implementer")
OVERRIDES_FILE = "roles.json"


@dataclass(frozen=True)
class RoleInfo:
    role: str
    configured: str  # co wpisano w agents.toml
    effective: str  # kto naprawdę wykona rolę
    reason: str  # "" gdy bez zmian, inaczej: override, fallback albo brak agenta

    @property
    def changed(self) -> bool:
        return self.effective != self.configured


@dataclass
class Config:
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    # kolejność prób, gdy agent roli nie jest zainstalowany (potem reszta agentów w kolejności z pliku)
    fallback_order: tuple[str, ...] = ()
    role_overrides: dict[str, str] = field(default_factory=dict)  # wybór użytkownika, np. z TUI
    model_overrides: dict[str, str] = field(default_factory=dict)  # agent -> model, np. z TUI
    _base_agents: dict[str, AgentConfig] | None = field(default=None, repr=False, compare=False)

    def available(self, name: str) -> bool:
        agent = self.agents.get(name)
        return agent is not None and agent.is_available()

    def _candidates(self, role: str) -> list[str]:
        order = [n for n in self.fallback_order if n in self.agents]
        order += [n for n in self.agents if n not in order]
        usable = [n for n in order if self.agents[n].headless and self.available(n)]
        want = "cheap" if role == "implementer" else "strong"  # planner i reviewer wolą mocny model, worker tani
        return sorted(usable, key=lambda n: self.agents[n].tier != want)  # sort jest stabilny

    def role_info(self, role: str) -> RoleInfo:
        configured = self.roles.get(role, "")
        chosen = self.role_overrides.get(role, "")
        if chosen in self.agents:
            return RoleInfo(role, configured, chosen, "" if chosen == configured else "wybór użytkownika")
        if configured and self.available(configured):
            return RoleInfo(role, configured, configured, "")
        for name in self._candidates(role):
            return RoleInfo(role, configured, name, f"zastępczo, brak {configured or 'agenta'} w PATH")
        return RoleInfo(role, configured, configured, "żaden agent nie jest zainstalowany")

    def role_name(self, role: str) -> str:
        return self.role_info(role).effective

    def agent_for_role(self, role: str) -> AgentConfig:
        name = self.role_name(role)
        if name not in self.agents:
            raise KeyError(f"Rola {role} nie ma agenta: dodaj go w agents.toml albo wybierz w TUI (przycisk Role)")
        return self.agents[name]

    def reload_overrides(self, repo: str | Path) -> None:
        """Wczytuje <repo>/.orchestra/roles.json (zapisuje je TUI albo `orchestra roles`)."""
        if self._base_agents is None:
            self._base_agents = dict(self.agents)
        roles, models = read_overrides(repo)
        self.role_overrides = {r: a for r, a in roles.items() if a in self._base_agents}
        self.model_overrides = {a: m for a, m in models.items() if a in self._base_agents}
        for name, base in self._base_agents.items():
            model = self.model_overrides.get(name)
            # model wybrany użytkownikiem wygrywa ze zmienną środowiskową i default_model
            self.agents[name] = replace(base, default_model=model, model_env="") if model else base


def overrides_path(repo: str | Path) -> Path:
    return Path(repo) / ".orchestra" / OVERRIDES_FILE


def read_overrides(repo: str | Path) -> tuple[dict[str, str], dict[str, str]]:
    try:
        data = json.loads(overrides_path(repo).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}, {}
    roles, models = data.get("roles", {}), data.get("models", {})
    ok = isinstance(roles, dict) and isinstance(models, dict)
    return (dict(roles), dict(models)) if ok else ({}, {})


def save_overrides(repo: str | Path, *, roles: dict[str, str] | None = None, models: dict[str, str] | None = None) -> None:
    """Dopisuje wybór ról i modeli (pusta wartość kasuje wpis)."""
    cur_roles, cur_models = read_overrides(repo)
    for target, new in ((cur_roles, roles), (cur_models, models)):
        for key, value in (new or {}).items():
            if value:
                target[key] = value
            else:
                target.pop(key, None)
    path = overrides_path(repo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"roles": cur_roles, "models": cur_models}, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def user_config_paths(repo: str | Path | None = None) -> list[Path]:
    """Dodatkowe pliki z własnymi agentami: ~/.config/orchestra/agents.toml, potem <repo>/.orchestra/agents.toml."""
    user = os.environ.get("ORCHESTRA_USER_CONFIG") or str(Path.home() / ".config" / "orchestra" / "agents.toml")
    paths = [Path(user)]
    if repo is not None:
        paths.append(Path(repo) / ".orchestra" / "agents.toml")
    return [p for p in paths if p.is_file()]


def _merge(base: dict, extra: dict) -> dict:
    out = {**base, "agents": {**base.get("agents", {}), **extra.get("agents", {})},
           "roles": {**base.get("roles", {}), **extra.get("roles", {})}}
    if "fallback_order" in extra:
        out["fallback_order"] = extra["fallback_order"]
    return out


def load_config(path: str | Path = "agents.toml", repo: str | Path | None = None) -> Config:
    """Wczytuje agents.toml i dokłada pliki użytkownika (własni agenci i modele), jeśli istnieją."""
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    for extra in user_config_paths(repo):
        data = _merge(data, tomllib.loads(extra.read_text(encoding="utf-8")))
    agents = {
        name: AgentConfig(
            name=name,
            description=raw.get("description", ""),
            acp=tuple(raw.get("acp", ())),
            headless=tuple(raw.get("headless", ())),
            tier=raw.get("tier", "strong"),
            default_model=raw.get("default_model", ""),
            model_env=raw.get("model_env", ""),
            require_model=bool(raw.get("require_model", False)),
            env_passthrough=tuple(raw.get("env_passthrough", ())),
        )
        for name, raw in data.get("agents", {}).items()
    }
    roles = dict(data.get("roles", {}))
    for role, agent in roles.items():
        if agent not in agents:
            raise ValueError(f"Rola {role} wskazuje nieznanego agenta {agent}")
    cfg = Config(agents=agents, roles=roles, fallback_order=tuple(data.get("fallback_order", ())))
    if repo is not None:
        cfg.reload_overrides(repo)
    return cfg
