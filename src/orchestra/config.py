"""Load agent configuration from agents.toml."""

from __future__ import annotations

import json
import os
import shutil
import tomllib
from dataclasses import dataclass, field, replace
from pathlib import Path


class ModelRequired(ValueError):
    """The agent requires an explicitly set model (an ID from `cmd --list-models`) and none was provided."""


@dataclass(frozen=True)
class AgentConfig:
    name: str
    description: str = ""
    acp: tuple[str, ...] = ()
    headless: tuple[str, ...] = ()
    tier: str = "strong"
    default_model: str = ""
    model_env: str = ""  # name of the environment variable holding the model ID (for example ORCHESTRA_CMD_MODEL)
    require_model: bool = False  # when true, a missing model is a clear error instead of the agent's default model
    # Environment variables passed to an agent launched over ACP. The ACP SDK starts the process with a trimmed
    # environment (HOME, PATH, etc.), so for example COMMAND_CODE_API_KEY must be passed explicitly.
    env_passthrough: tuple[str, ...] = ()
    # Price per 1M tokens in USD, used to estimate the cost of agents that do not report it (for example cmd). 0 means unknown.
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0

    def binary(self) -> str:
        """First element of the headless command (or ACP when that is absent): the binary that must be on PATH."""
        argv = self.headless or self.acp
        return argv[0] if argv else ""

    def is_available(self) -> bool:
        """Whether the agent binary is installed (on PATH or as a path)."""
        binary = self.binary()
        return bool(binary) and shutil.which(binary) is not None

    def acp_env(self) -> dict[str, str]:
        """Variables from env_passthrough that are set in the current environment."""
        return {k: os.environ[k] for k in self.env_passthrough if os.environ.get(k)}

    def resolve_model(self, model: str | None = None) -> str:
        """Order: argument, model_env environment variable, default_model field."""
        env_value = os.environ.get(self.model_env, "").strip() if self.model_env else ""
        return (model or env_value or self.default_model).strip()

    def headless_argv(self, task: str, model: str | None = None) -> list[str]:
        """Return the headless-mode argv with the task and model substituted."""
        if not self.headless:
            raise ValueError(f"Agent {self.name} has no headless command")
        chosen = self.resolve_model(model)
        if self.require_model and not chosen and any("{model}" in part for part in self.headless):
            hint = f"the {self.model_env} variable" if self.model_env else "the default_model field in agents.toml"
            raise ModelRequired(
                f"Agent {self.name} requires a model: set {hint} to an ID from `cmd --list-models` "
                f"(for example deepseek/deepseek-v4.1-flash) or pass a model argument."
            )
        out: list[str] = []
        for part in self.headless:
            if part == "{model}" and not chosen:
                # no model: also drop the preceding --model/-m flag
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
    configured: str  # what was written in agents.toml
    effective: str  # who actually performs the role
    reason: str  # "" when unchanged, otherwise: override, fallback or no agent

    @property
    def changed(self) -> bool:
        return self.effective != self.configured


@dataclass
class Config:
    agents: dict[str, AgentConfig] = field(default_factory=dict)
    roles: dict[str, str] = field(default_factory=dict)
    # order of attempts when the role's agent is not installed (then the rest of the agents in file order)
    fallback_order: tuple[str, ...] = ()
    # environment variables passed to test_cmd and integration_test_cmd beyond the safe set
    test_env_passthrough: tuple[str, ...] = ()
    role_overrides: dict[str, str] = field(default_factory=dict)  # user choice, for example from the TUI
    model_overrides: dict[str, str] = field(default_factory=dict)  # agent -> model, for example from the TUI
    _base_agents: dict[str, AgentConfig] | None = field(default=None, repr=False, compare=False)

    def available(self, name: str) -> bool:
        agent = self.agents.get(name)
        return agent is not None and agent.is_available()

    def _candidates(self, role: str) -> list[str]:
        order = [n for n in self.fallback_order if n in self.agents]
        order += [n for n in self.agents if n not in order]
        usable = [n for n in order if self.agents[n].headless and self.available(n)]
        want = "cheap" if role == "implementer" else "strong"  # planner and reviewer prefer a strong model, the worker a cheap one
        return sorted(usable, key=lambda n: self.agents[n].tier != want)  # sort is stable

    def role_info(self, role: str) -> RoleInfo:
        configured = self.roles.get(role, "")
        chosen = self.role_overrides.get(role, "")
        if chosen in self.agents:
            return RoleInfo(role, configured, chosen, "" if chosen == configured else "user choice")
        if configured and self.available(configured):
            return RoleInfo(role, configured, configured, "")
        for name in self._candidates(role):
            return RoleInfo(role, configured, name, f"fallback, missing {configured or 'agent'} in PATH")
        return RoleInfo(role, configured, configured, "no agent is installed")

    def role_name(self, role: str) -> str:
        return self.role_info(role).effective

    def agent_for_role(self, role: str) -> AgentConfig:
        name = self.role_name(role)
        if name not in self.agents:
            raise KeyError(f"Role {role} has no agent: add one in agents.toml or choose one in the TUI (Role button)")
        return self.agents[name]

    def reload_overrides(self, repo: str | Path) -> None:
        """Load <repo>/.orchestra/roles.json (written by the TUI or `orchestra roles`)."""
        if self._base_agents is None:
            self._base_agents = dict(self.agents)
        roles, models = read_overrides(repo)
        self.role_overrides = {r: a for r, a in roles.items() if a in self._base_agents}
        self.model_overrides = {a: m for a, m in models.items() if a in self._base_agents}
        for name, base in self._base_agents.items():
            model = self.model_overrides.get(name)
            # a model chosen by the user wins over the environment variable and default_model
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
    """Append the choice of roles and models (an empty value deletes the entry)."""
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
    """Additional files with custom agents: ~/.config/orchestra/agents.toml, then <repo>/.orchestra/agents.toml."""
    user = os.environ.get("ORCHESTRA_USER_CONFIG") or str(Path.home() / ".config" / "orchestra" / "agents.toml")
    paths = [Path(user)]
    if repo is not None:
        paths.append(Path(repo) / ".orchestra" / "agents.toml")
    return [p for p in paths if p.is_file()]


def _merge(base: dict, extra: dict) -> dict:
    out = {**base, "agents": {**base.get("agents", {}), **extra.get("agents", {})},
           "roles": {**base.get("roles", {}), **extra.get("roles", {})}}
    for key in ("fallback_order", "test_env_passthrough"):
        if key in extra:
            out[key] = extra[key]
    return out


def load_config(path: str | Path = "agents.toml", repo: str | Path | None = None) -> Config:
    """Load agents.toml and merge in the user files (custom agents and models) if they exist."""
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
            price_in_per_mtok=float(raw.get("price_in_per_mtok", 0.0)),
            price_out_per_mtok=float(raw.get("price_out_per_mtok", 0.0)),
        )
        for name, raw in data.get("agents", {}).items()
    }
    roles = dict(data.get("roles", {}))
    for role, agent in roles.items():
        if agent not in agents:
            raise ValueError(f"Role {role} points to an unknown agent {agent}")
    cfg = Config(agents=agents, roles=roles, fallback_order=tuple(data.get("fallback_order", ())),
                 test_env_passthrough=tuple(data.get("test_env_passthrough", ())))
    if repo is not None:
        cfg.reload_overrides(repo)
    return cfg
