# agent-orchestra

Orkiestrator agentów kodujących z hierarchią kosztową. Mocny model (Claude) planuje, robi research i recenzuje diffy, a tani worker z commandcode.ai implementuje proste ficzery w izolowanym git worktree. Całość widać w terminalu (Textual).

## Założenia

- Reużycie gotowców: oficjalne Python SDK protokołu ACP, FastMCP, Textual, SQLite, git worktree.
- Wspólne skille: `.agents/skills/` jest źródłem prawdy, `.claude/skills` to symlink. `AGENTS.md` jest wspólną instrukcją, `CLAUDE.md` importuje `@AGENTS.md`.
- Konfiguracja agentów i ról w `agents.toml`.

## Status etapów

- [x] 1. Szkielet repo i konfiguracja
- [x] 2. Serwer MCP z narzędziem `delegate`
- [x] 3. Warstwa ACP i log zdarzeń w SQLite
- [ ] 4. TUI w Textual

## Uruchomienie

```
uv venv && uv pip install -e ".[dev]"
.venv/bin/pytest
```

## Narzędzie MCP `delegate`

Rejestracja w Claude Code (stdio):

```
claude mcp add orchestra -e ORCHESTRA_REPO=$PWD -e ORCHESTRA_CONFIG=$PWD/agents.toml -- $PWD/.venv/bin/orchestra-mcp
```

Narzędzie tworzy worktree `.orchestra/worktrees/<id>` na gałęzi `orchestra/<id>`, uruchamia workera headless (domyślnie rola `implementer`, czyli `cmd -p ... --output-format json`), odpala `test_cmd` (bez powłoki) i zwraca diff względem bazy, wynik testów i zużycie tokenów. Worker `fake` zapisuje pliki z `fake_files` i służy do testów.

## Warstwa ACP i log zdarzeń

- `orchestra.acp_client.AgentSession`: uruchamia agenta (`spawn_agent_process` z oficjalnego SDK), robi `initialize`, `session/new` (z `cwd` worktree i `mcpServers`), `session/prompt`, anulowanie. Aktualizacje `session/update` są normalizowane do zdarzeń: `agent_message`, `agent_thought`, `tool_call`, `tool_update`, `context`, `permission_request`, `permission_response`, `prompt_start`, `prompt_end`, `error`.
- Zużycie tokenów z `PromptResponse.usage` (jeśli agent je zwraca) trafia do kolumn `tokens_in` i `tokens_out`, koszt z `usage_update` do `cost`.
- Uprawnienia: domyślnie `reject_all`; handler jest podmienialny (TUI podstawi okno akceptacji).
- `orchestra.store.EventStore`: SQLite w trybie WAL, tabele `events` i `messages`. Serwer MCP wystawia też `post_message` i `read_inbox` jako skrzynkę między agentami.
- `orchestra.testing.mock_agent`: minimalny agent ACP do testów i demo (`python -m orchestra.testing.mock_agent`).
