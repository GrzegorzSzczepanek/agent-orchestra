# agent-orchestra

Orkiestrator agentów kodujących z hierarchią kosztową. Mocny model (Claude) planuje, robi research i recenzuje diffy, a tani worker z commandcode.ai implementuje proste ficzery w izolowanym git worktree. Całość widać w terminalu (Textual).

## Szybki start (5 kroków)

1. **Instalacja:** `scripts/setup.sh` (tworzy `.venv`, instaluje pakiet, pyta o `npm i -g command-code`, na końcu uruchamia diagnostykę). Dodaj `--yes`, żeby nie pytało o npm.
2. **Logowanie (ręcznie):** `cmd login` (otwiera przeglądarkę). Na serwerze bez przeglądarki zamiast tego `export COMMAND_CODE_API_KEY=...`.
3. **Model:** `cmd --list-models`, potem `export ORCHESTRA_CMD_MODEL=<id z listy>`, np. `deepseek/deepseek-v4.1-flash`. Pełne ID albo część po ostatnim `/` (`glm-5.3-flash`), wielkość liter nie ma znaczenia. Bez modelu worker zwraca jasny błąd konfiguracji.
4. **Test na żywo:** `.venv/bin/orchestra doctor --smoke` (jedno zapytanie, zużywa kilka groszy kredytów). Wklej zawartość `orchestra-doctor.json` do rozmowy z Claude: raport nie zawiera sekretów (maskowane są `COMMAND_CODE_API_KEY`, tokeny i wzorce kluczy), więc dopasuję parser i konfigurację do tego, co faktycznie zwraca Twoje konto.
5. **Praca:** albo zarejestruj narzędzie `delegate` w Claude Code (niżej), albo użyj `orchestra plan` i `orchestra run`.

## Polecenia

```
orchestra doctor [--smoke] [--model ID] [--no-others] [--json plik]   # diagnostyka, PASS/FAIL/SKIP/WARN
orchestra plan "cel" [--via headless|acp] [--out plan.json]            # planner układa DAG zadań w JSON
orchestra run plan.json [--parallel 2] [--rounds 0] [--max-failures 2] [--token-budget 400000]
orchestra-tui --repo . --agents claude,commandcode                     # TUI (Textual); --demo pokazuje agenty mock
orchestra-mcp                                                          # serwer MCP z narzędziem delegate
```

Rejestracja serwera MCP w Claude Code (Claude planuje i recenzuje, a proste zadania oddaje workerowi):

```
claude mcp add orchestra -e ORCHESTRA_REPO=$PWD -e ORCHESTRA_CONFIG=$PWD/agents.toml -- $PWD/.venv/bin/orchestra-mcp
```

## Jak działa hierarchia kosztowa

- **Plan:** rola `planner` (domyślnie `claude -p`) zwraca JSON zgodny ze schematem `Plan` (pydantic): zadania z polami `id`, `description`, `files`, `acceptance`, `test_cmd`, `difficulty` (`easy`, `medium`, `hard`), `depends_on`. Zależności i cykle waliduje `graphlib`. Błędna odpowiedź dostaje jedną poprawkę.
- **Wykonanie:** scheduler uruchamia zadania równolegle (1 do 3, domyślnie 2). Każde zadanie dostaje worktree odgałęziony od gałęzi integracyjnej `orchestra/run-...`, więc zadania zależne widzą kod swoich zależności.
- **Bramka testów:** `test_cmd` zadania musi przejść. Dopiero wtedy gałąź zadania jest scalana (`--no-ff`) do gałęzi integracyjnej. Konflikt scalenia liczy się jak porażka. Wynik jest do przeglądu na gałęzi integracyjnej, `main` zostaje nietknięty.
- **Eskalacja:** po 2 kolejnych porażkach taniego workera zadanie dostaje jedną próbę od roli `planner` (mocny model). Zadania `hard` idą od razu do mocnego modelu. Błędy krytyczne (zła konfiguracja, brak logowania, brak kredytów) kończą zadanie bez ponawiania i zatrzymują resztę przebiegu.
- **Budżet:** tokeny (wejście plus wyjście) na zadanie liczone z logu SQLite (`--token-budget`, 0 wyłącza). Limit kosztu w USD działa tylko wtedy, gdy agent raportuje koszt (Claude w trybie JSON go podaje, `cmd` według dokumentacji nie).
- **Dyskusja:** domyślnie wyłączona (`--rounds 0`, maksimum 2). Po zielonych testach rola `reviewer` ocenia diff (`APPROVE` albo `CHANGES: ...`), uwagi trafiają do wspólnej skrzynki w SQLite i do kolejnej próby. Po wyczerpaniu rund zielone testy wygrywają, a nierozwiązane uwagi są w raporcie. Badania nad debatą wieloagentową dają mieszane wyniki i wyższy koszt tokenów, więc domyślnie jej nie używamy.

## Ważne szczegóły

- **Środowisko agentów ACP:** SDK ACP uruchamia agenta z okrojonym środowiskiem (HOME, PATH i kilka innych). Sekrety trzeba przekazać jawnie przez `env_passthrough` w `agents.toml` (dla `cmd`: `COMMAND_CODE_API_KEY`, `CMD_ZDR`, `CMD_LOCAL_ONLY`). Dla Claude i Codex lista jest celowo pusta, żeby nie przełączać logowania subskrypcją na rozliczanie przez API.
- **Tryb headless** (`delegate`, `plan`, `run`) dziedziczy pełne środowisko procesu. Worker `cmd` dostaje `--yolo`, bo izolacją jest worktree. Nie uruchamiaj go na repozytorium z sekretami, których worker nie powinien widzieć.
- **Plan Go** za 1 USD: tylko CLI, bez Provider API (nie da się podpiąć przez `ANTHROPIC_BASE_URL`), limity według strony planu 3 USD na 5 godzin, 6 USD na 7 dni, 10 USD miesięcznie. Strona planu nie mówi, czy Go obejmuje tryb headless i ACP, co rozstrzyga `orchestra doctor --smoke`.
- **Statusy workera** (`delegate`): `ok`, `tests_failed`, `worker_failed`, `config_error`, `worker_not_found`, `timeout` oraz z kodów wyjścia `cmd`: `auth_failed` (3), `permission_denied` (4), `rate_limited` (5), `connection_error` (6), `server_error` (7), `max_turns` (8), `no_response` (9), `insufficient_credits` (10), `interrupted` (130).
- Sprzątanie po przebiegu: `git worktree prune`, a gałęzie `orchestra/*` usuń po scaleniu.

## Struktura

```
agents.toml                 agenci, role, env_passthrough, model workera (zmienna ORCHESTRA_CMD_MODEL)
AGENTS.md, CLAUDE.md        wspólne instrukcje (CLAUDE.md importuje @AGENTS.md)
.agents/skills/             wspólne skille (SKILL.md), .claude/skills to symlink
src/orchestra/              commandcode, delegate, mcp_server, acp_client, store, doctor, plan, scheduler,
                            agents_io, controller, tui, cli
scripts/setup.sh            instalacja i diagnostyka
tests/                      pytest (71 testów), fixtures z prawdziwego wyjścia cmd 1.93.2
```

## Zweryfikowane vs niezweryfikowane

**Zweryfikowane na prawdziwym `cmd` 1.93.2** (zainstalowanym z npm w środowisku budowy, bez konta, więc bez logowania):
- Instalacja `npm i -g command-code` (Node 22), `cmd --help`, `cmd acp --help`.
- `cmd --list-models` działa bez logowania i zwraca 105 modeli plus sekcję `Decision models (headless only)`, którą pomijamy. Prawdziwe wyjście jest fixture'em testów. Model `deepseek/deepseek-v4.1-flash` istnieje i jest domyślny.
- `cmd status` bez logowania zwraca kod 1. `cmd -p ... --output-format json` bez logowania zwraca kod 3 i jedną linię `{"type":"result","subtype":"error",...,"usage":{inputTokens,outputTokens,cacheReadTokens,cacheWriteTokens},"durationMs","finalText","error"}`. Flagi `--skip-onboarding`, `--yolo`, `--max-turns` są akceptowane.
- `cmd acp` przez oficjalne SDK: `initialize` działa bez logowania (Command Code 1.93.2, protokół v1, `mcp_capabilities.http=true`, `sse=false`, metoda logowania `terminal`), a `session/new` bez logowania kończy się błędem "Not authenticated". Okrojone środowisko procesu ACP jest sprawdzone testem.
- `orchestra doctor` uruchomiony przeciwko prawdziwemu `cmd` (bez logowania) poprawnie zgłasza PASS dla binarki, listy modeli i ACP initialize oraz FAIL dla logowania i `session/new`.

**Zweryfikowane testami (71, pytest)** na mockowych binarkach i agentach: parser NDJSON (zdarzenia, nieznane ramki, błędne linie, brak pól opcjonalnych, format `claude -p`), mapowanie kodów wyjścia, wymóg modelu, doctor (PASS, FAIL, SKIP, WARN, maskowanie sekretów), planista i walidacja DAG-a, scheduler (równoległość, scalanie do gałęzi integracyjnej, bramka testów, eskalacja, blokowanie zależnych, budżet tokenów, limit rund, konflikt scalenia, błąd krytyczny), CLI `plan` i `run` od końca do końca, serwer MCP, klient ACP, TUI (Pilot).

**Niezweryfikowane, wymaga Twojego konta lub agentów** (wklej raport z `orchestra doctor --smoke`):
- Udana odpowiedź `cmd -p` po zalogowaniu: kształt ramek `event` i linii wyniku przy `subtype: success` (parser zakłada pola z dokumentacji i ignoruje nieznane) oraz to, czy plan Go w ogóle obejmuje headless i ACP.
- Czy `cmd acp` zwraca `usage` w `PromptResponse` (dokumentacja tego nie obiecuje, kolumny tokenów w TUI mogą zostać zerowe). W trybie headless tokeny są w linii wyniku.
- Kod wyjścia `cmd` przy nieznanym modelu (komunikat błędu zweryfikowany, kod nie).
- `claude-agent-acp`, `codex-acp`, `agy acp`, `opencode acp` oraz komendy headless `agy -p`, `codex exec`, `opencode run`. Format `claude -p --output-format json` (pole `result`, `total_cost_usd`) pochodzi z dokumentacji, nie z uruchomienia. Domyślne `env_passthrough` dla nich jest puste.
- Regulamin: uruchamianie niezmodyfikowanego `claude` na własnym koncie jest zgodne z dokumentacją Anthropic według raportu research, ale strona Legal and compliance zmienia się często, więc sprawdź ją przed publikacją narzędzia.

## TODO

- Wspólne skille i MCP wstrzykiwane przez `session/new` do każdego agenta (mechanizm `mcp_servers` jest gotowy, brakuje konfiguracji).
- Sandbox Docker dla trybów YOLO.
- Kontynuacja pracy w tym samym worktree po uwagach recenzenta (teraz nowa próba startuje od czystego worktree z poprzednim patchem w prompcie).
- Koszt w USD liczony z cennika modeli (teraz tylko z danych agenta).
- Podgląd przebiegu `orchestra run` w TUI (zdarzenia scheduler i `task_attempt` trafiają już do wspólnego logu).
- Fragmenty strumienia odpowiedzi dzielące się na granicy odpytywania mogą zostać wypisane w osobnych liniach.
