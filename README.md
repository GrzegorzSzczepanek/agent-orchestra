# agent-orchestra

Orkiestrator agentów kodujących z hierarchią kosztową. Mocny model (Claude) planuje, robi research i recenzuje diffy, a tani worker z commandcode.ai implementuje proste ficzery w izolowanym git worktree. Całość widać w terminalu (Textual).

## Założenia

- Reużycie gotowców: oficjalne Python SDK protokołu ACP, FastMCP, Textual, SQLite, git worktree.
- Wspólne skille: `.agents/skills/` jest źródłem prawdy, `.claude/skills` to symlink. `AGENTS.md` jest wspólną instrukcją, `CLAUDE.md` importuje `@AGENTS.md`.
- Konfiguracja agentów i ról w `agents.toml`.

## Status etapów

- [x] 1. Szkielet repo i konfiguracja
- [x] 2. Serwer MCP z narzędziem `delegate`
- [ ] 3. Warstwa ACP i log zdarzeń w SQLite
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
