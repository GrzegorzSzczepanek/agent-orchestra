"""Ładowanie konfiguracji agentów z agents.toml."""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class AgentConfig:
    name: str
    description: str = ""
    acp: tuple[str, ...] = ()
    headless: tuple[str, ...] = ()
    tier: str = "strong"
    default_model: str = ""

    def headless_argv(self, task: str, model: str | None = None) -> list[str]:
        """Zwraca argv trybu headless z podstawionym zadaniem i modelem."""
        if not self.headless:
            raise ValueError(f"Agent {self.name} nie ma komendy headless")
        chosen = model or self.default_model
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
        )
        for name, raw in data.get("agents", {}).items()
    }
    roles = dict(data.get("roles", {}))
    for role, agent in roles.items():
        if agent not in agents:
            raise ValueError(f"Rola {role} wskazuje nieznanego agenta {agent}")
    return Config(agents=agents, roles=roles)
