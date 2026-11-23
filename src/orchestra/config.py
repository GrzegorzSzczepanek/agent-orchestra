"""Ładowanie konfiguracji agentów z agents.toml."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
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


@dataclass(frozen=True)
class Config:
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)

    def agent_for_role(self, role: str) -> AgentConfig:
        return self.agents[self.roles[role]]


def load_config(path: str | Path = "agents.toml") -> Config:
    data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
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
    return Config(agents=agents, roles=roles)
