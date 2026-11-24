# agent-orchestra

Orkiestrator agentów kodujących z hierarchią kosztową. Mocny model (Claude) planuje, robi research i recenzuje diffy, a tani worker z commandcode.ai implementuje proste ficzery w izolowanym git worktree. Całość widać w terminalu (Textual).

## Szybki start (5 kroków)

1. **Instalacja:** `scripts/setup.sh` (tworzy `.venv`, instaluje pakiet, pyta o `npm i -g command-code`, na końcu uruchamia diagnostykę). Dodaj `--yes`, żeby nie pytało o npm. Dla roli planisty doinstaluj pod tym samym Node: `npm i -g @anthropic-ai/claude-code @agentclientprotocol/claude-agent-acp` (adapter wymaga Node >=22; nvm trzyma globalne paczki osobno dla każdej wersji Node, więc po `nvm use` zainstaluj ponownie).
2. **Logowanie (ręcznie):** `cmd login` (otwiera przeglądarkę). Na serwerze bez przeglądarki zamiast tego `export COMMAND_CODE_API_KEY=...`.
3. **Model:** `cmd --list-models`, potem `export ORCHESTRA_CMD_MODEL=<id z listy>`, np. `deepseek/deepseek-v4.1-flash`. Pełne ID albo część po ostatnim `/` (`glm-5.3-flash`), wielkość liter nie ma znaczenia. Bez modelu worker zwraca jasny błąd konfiguracji.
4. **Test na żywo:** `.venv/bin/orchestra doctor --smoke` (jedno zapytanie, zużywa kilka groszy kredytów). Wklej zawartość `orchestra-doctor.json` do rozmowy z Claude: raport nie zawiera sekretów (maskowane są `COMMAND_CODE_API_KEY`, tokeny i wzorce kluczy), więc dopasuję parser i konfigurację do tego, co faktycznie zwraca Twoje konto.
5. **Praca:** najprościej `scripts/go.sh "cel zadania"` (plan i wykonanie jednym poleceniem). Alternatywnie zarejestruj narzędzie `delegate` w Claude Code (niżej) albo użyj `orchestra plan` i `orchestra run`.

## Polecenia

```
orchestra doctor [--smoke] [--model ID] [--no-others] [--json plik]   # diagnostyka, PASS/FAIL/SKIP/WARN
orchestra plan "cel" [--via headless|acp] [--out plan.json]            # planner układa DAG zadań w JSON
orchestra run plan.json [--parallel 2] [--rounds 0] [--max-failures 2] [--token-budget 400000]
orchestra-tui --repo . --agents claude,commandcode                     # TUI (Textual); --demo pokazuje agenty mock
orchestra-mcp                                                          # serwer MCP: delegate, run_plan, run_status
orchestra watch [--repo .]                                             # podgląd przebiegów na żywo (TUI)

scripts/go.sh "cel" [--repo KAT] [--parallel N] [--claude] [--plan-only] [--tui] [--watch]   # plan i wykonanie jednym poleceniem
```

`scripts/go.sh` domyślnie planuje tanim `commandcode`, więc działa także wtedy, gdy Claude ma wyczerpany limit. Dodaj `--claude`, żeby planował Claude. `--tui` otwiera TUI, `--plan-only` zatrzymuje się po planie. Model workera bierze z `ORCHESTRA_CMD_MODEL`, a gdy zmienna jest pusta, ustawia `deepseek/deepseek-v4.1-flash`.

## Praca z Claude Code (główny scenariusz)

W katalogu repo jest `.mcp.json` i `.claude/settings.json`, więc po `scripts/setup.sh` wystarczy uruchomić `claude` w tym katalogu: serwer `orchestra` jest włączony od razu. Narzędzia MCP:

- `run_plan(goal, tasks)`: Claude rozbija trudne zadanie na małe (id, opis, pliki, kryteria, `test_cmd`, trudność, zależności), a tani worker wykonuje je równolegle w osobnych worktree z bramką testów. Zwraca `run_id` od razu, praca idzie w tle. Zadania nieudane wracają do Claude'a (bez eskalacji do `claude -p`).
- `run_status(run_id, wait_s)`: stan zadań, a po zakończeniu raport i gałąź integracyjna `orchestra/run-<id>` do recenzji (`git diff HEAD...orchestra/run-<id>`).
- `delegate`: pojedyncze proste zadanie, `post_message` i `read_inbox`: skrzynka między agentami.

Skille `orchestrate-hard-task` (rozbijanie i rozdawanie) i `delegate-simple-tasks` mówią Claude'owi, kiedy i jak z tego korzystać. Przykład: napisz w Claude Code "rozbij to na zadania i rozdaj workerom: ...".

**Interaktywne TUI (bez uczenia się komend):** `orch` (globalny launcher, `scripts/orch`) otwiera Claude'a i `orchestra-tui` obok siebie w jednym oknie (poza tmux sam startuje sesję tmux z dwoma panelami, myszka działa, wyjście z Claude'a zamyka sesję). Wszystko jest widoczne od razu:

- **Tabela po lewej:** agenci (sesje ACP), przebieg zadań i wiadomości. Zadania z `run_plan` pojawiają się w tabeli od razu jako `○ pending`, potem `● working`, `✓ done`, `✗ failed` z licznikiem prób i tokenami. Zaznaczone zadanie pokazuje kroki workera na żywo (komendy, zapisy plików, wyniki), a nieudana próba pokazuje przyczynę i końcówkę wyniku testów.
- **Przyciski u góry** (i te same F-klawisze): `＋ Agent` (Ctrl+N, nowy agent dowolnego typu z `agents.toml`, w własnym worktree), `✉ Wiadomość` (F3), `✎ Wskazówka` (F4, trafia do następnej próby workera), `🔎 Szczegóły` (F5, błąd, `test_cmd`, pełny wynik testów, pliki), `⛔ Anuluj przebieg` (F6, zabija pracujące workery), `± Diff` (F2). Pod logiem jest podpowiedź, co robi pole tekstowe dla zaznaczonego wiersza: dla agenta to prompt, dla zadania wskazówka. Skróty tekstowe `/hint`, `/detail`, `/cancel`, `/msg` robią to samo, ale nie są potrzebne.
- **Komunikacja agent z agentem:** każdy agent startowany z TUI ma podpięty serwer MCP `orchestra` (`post_message`, `read_inbox`, `list_agents`, przez `session/new`), zweryfikowane na prawdziwym `cmd acp`. Wiadomość do działającego agenta (od Ciebie przyciskiem F3 albo od innego agenta) trafia do niego jako kolejny prompt z poleceniem, jak odpowiedzieć. Po 10 wiadomościach agentów z rzędu bez Ciebie przekazywanie się wstrzymuje. Cała rozmowa, razem z uwagami recenzenta, jest w wierszu `✉ wiadomości`.
- **Recenzent:** `run_plan(..., review_rounds=1)` (1 do 2) po zielonych testach daje diff do oceny roli `reviewer` (domyślnie Claude, zużywa jego limit), a uwagi wracają do workera. Zweryfikowane tylko na mockach, nie na prawdziwym Claude.

Zadanie w `run_plan` ma pole `cwd` (podkatalog repo, w którym działa worker i `test_cmd`, np. `code`).

**Podgląd zadań na żywo (tylko odczyt):** `orch --simple-watch` albo w drugim terminalu `.venv/bin/orchestra watch` (albo `scripts/go.sh --watch`). Góra: tabela zadań (status, worker, próby, tokeny, zależności), dół: strumień zdarzeń. `n` przełącza przebieg, `q` wychodzi. Czyta wspólny log `.orchestra/events.db`, więc widzi też `delegate`.

**Node:** `cmd` i adapter ACP wymagają Node >= 22. `orchestra`, `orchestra-mcp` i `orchestra-tui` same dopisują do PATH Node 22+ z nvm, gdy domyślny jest starszy albo `cmd` nie jest w PATH (`.nvmrc` zawiera 22). Model workera: `default_model` w `agents.toml` (`deepseek/deepseek-v4.1-flash`), zmienna `ORCHESTRA_CMD_MODEL` ma pierwszeństwo.

Użycie w innym repozytorium (rejestracja globalna, ścieżki bezwzględne):

```
claude mcp add orchestra -s user -e ORCHESTRA_REPO=. -e ORCHESTRA_CONFIG=$PWD/agents.toml -- $PWD/.venv/bin/orchestra-mcp
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
scripts/go.sh               plan i wykonanie zadania jednym poleceniem
tests/                      pytest (107 testów), fixtures z prawdziwego wyjścia cmd 1.93.2 i claude 2.1.331
```

## Narzut tokenów i próg delegowania

- Stały narzut wejścia `cmd -p` na krótkim promptcie (cmd 1.93.2, deepseek-v4.1-flash): 15261 tokenów, z czego 5888 to cache read. Efekt `--no-skills --safe-mode` jest niepotwierdzony: kolejny pomiar (jedno wywołanie na wariant) dał `--no-skills` 13827, `--safe-mode` 15294, a `--no-skills --safe-mode --no-session --effort low` 15261, czyli w granicach szumu (cache read waha się od 4096 do 7168). Flagi są w `agents.toml` (tylko headless), ale nie traktuj ich jako sprawdzonej oszczędności.
- Nie ma flagi wyłączającej bazowy system prompt, definicje narzędzi ani pamięć `AGENTS.md`. Pamięć jest częścią systemu promptu i kosztuje na każdym żądaniu, więc w worktree repo z dużym `AGENTS.md` narzut rośnie. `--no-skills` nie usuwa pamięci.
- Przy rozliczaniu per token DeepSeek V4.1 Flash jest około 20 razy tańszy od Claude Sonnet 4.6 ($0.15/$0.6 vs $3/$15 za 1M). Nawet zmierzony przebieg zużywający 202 tys. tokenów workera i planisty kosztuje około 0.03 USD, więc delegowanie opłaca się dla każdego zadania z jasnym `test_cmd`, które nie wymaga dyskusji. Przy subskrypcji Claude koszt krańcowy to limity planu, nie dolary: deleguj zadania ograniczone do 1-3 plików z konkretnym testem, a planowanie, projektowanie i recenzję zostaw mocnemu modelowi.
- Pomiar Claude vs worker na tym samym zadaniu (`truncate_slug`, 4 testy, jeden plik): sam Claude (`claude -p`, 5 tur) 0.0752 USD, 6 tokenów wejścia plus 15045 cache write i 53254 cache read, 964 wyjścia. Plan od Claude (jedno wywołanie, 1 zadanie) 0.0724 USD, a worker `cmd` 60253/755 tokenów (1 próba, bez kosztu w USD). Przy jednym małym zadaniu sam plan kosztuje tyle, ile zrobienie go samym Claude'em, więc delegowanie opłaca się dopiero, gdy jeden plan obejmuje kilka zadań (koszt planu rozkłada się na zadania) albo zadanie jest duże. Stały narzut samego Claude'a to około 20 tys. tokenów na wywołanie (z cache).
- Zmierzone przebiegi: proste zadanie z ręcznym planem 63972/462 tokenów workera (około 0.010 USD), to samo zadanie z planistą `cmd` 29404/521 tokenów planisty i 173288/4262 workera (około 0.033 USD razem, bez rabatu cache).

## Zweryfikowane vs niezweryfikowane

**Zweryfikowane na prawdziwym środowisku** (macOS, Node 22.23.5, `cmd` 1.93.2, `claude` 2.1.331, `claude-agent-acp` 0.110.1, `opencode` 1.14.25):
- `orchestra doctor`: 10 PASS, 0 FAIL, 5 SKIP. PASS dla `cmd` (logowanie, 105 modeli, ACP initialize i `session/new`), dla `claude-agent-acp` ACP initialize oraz dla `opencode` ACP initialize. SKIP dla `codex` i `agy`, których nie ma w PATH.
- `cmd -p --output-format json` po zalogowaniu zwraca 11 ramek `event` i końcową linię `result` (`subtype: success`, `usage.inputTokens`, `outputTokens`, `cacheReadTokens`, `sessionId`, `stopReason`, `finalText`). Prawdziwe wyjście jest fixture'em testów, podobnie jak odpowiedź bez logowania. W przypadku crashu `cmd` Node obcina stderr potoku do około 64 KB, więc stack trace może zginąć: doctor ma na to fallback (stary `command-code` 0.52.5 pod Node 20 bez pola `engines`).
- `cmd acp` zwraca `usage` w odpowiedzi na prompt: w TUI widać tokeny 17157/3. Nie zgłasza natomiast kosztu w USD, więc kolumna `$` pozostaje zerowa.
- TUI (`orchestra-tui`) na repo testowym z prawdziwym `cmd acp`: tabela agentów, status, strumień zdarzeń, treść odpowiedzi i tokeny działają. Naprawione: do promptu trzeba było dwóch Tabów (fokus przystawał na RichLog), teraz jeden. Do poprawy: tabela miesza prawdziwych agentów z wierszami z logu (`role:planner`, `scheduler`, `worker:commandcode`, status `· ?`), kolumna `in` nie liczy tokenów cache, brak kosztu z cennika, a fragmenty strumienia mogą łamać się na granicy odpytywania.
- Planowanie i wykonanie od końca do końca z planistą Claude (`claude -p`, 10 s, 0.0724 USD): planista zwrócił poprawny DAG z jednym zadaniem, worker `cmd` zaimplementował funkcję, bramka `pytest` przeszła, gałąź scalona `--no-ff` do `orchestra/run-<id>`, testy na gałęzi integracyjnej zielone (4 passed). Log SQLite zawiera teraz `role_attempt` planisty i `task_attempt` workera.
- `claude -p --output-format json` przy wyczerpanym limicie konta zwraca kod 0, `subtype: success`, `is_error: true` i `api_error: usage_limit_reached`. Parser i `headless_ask` traktują to jako błąd roli z czytelnym komunikatem (wcześniej limit wyglądał jak poprawna odpowiedź). Prawdziwa odpowiedź jest fixture'em testów.
- Doctor przy crashu `cmd` (stack trace z `cli.mjs`) odczytuje `engines.node` z pakietu npm i wypisuje `wymagany Node >=N, masz vX`, zamiast wstawiać surową linię błędu jako wersję.

**Zweryfikowane testami (84, pytest)** na mockowych binarkach i agentach oraz na prawdziwych, zanonimizowanych fixture'ach: parser NDJSON (ramki event, wynik sukcesu i błędu, nieznane ramki, błędne linie, format `claude -p`, `is_error` przy `subtype: success`), mapowanie kodów wyjścia, wymóg modelu, doctor (PASS, FAIL, SKIP, WARN, crash Node, maskowanie sekretów), planista i walidacja DAG-a, scheduler (równoległość, scalanie do gałęzi integracyjnej, bramka testów, eskalacja, blokowanie zależnych, budżet tokenów, limit rund, konflikt scalenia, błąd krytyczny), CLI `plan` i `run` od końca do końca, logowanie zużycia ról, serwer MCP, klient ACP, TUI (Pilot).

- `run_plan` i `run_status` przez prawdziwy serwer MCP (stdio) z workerem `cmd` (deepseek-v4.1-flash): 3 zadania, trzecie zależne od dwóch pierwszych, równolegle 2. Wszystkie `done`, gałąź integracyjna z 6 commitami, 5 testów zielonych (około 160 tys. tokenów wejścia). `claude mcp list` pokazuje `orchestra ✔ Connected`. Nie sprawdzone: wywołanie tych narzędzi przez Claude w sesji interaktywnej.

**Niezweryfikowane**:
- Rola recenzenta (`--rounds 1` lub więcej) na prawdziwym Claude: nie była uruchamiana. Planista przez `--via acp` (`claude-agent-acp`) też nie: sprawdzony jest tylko ACP initialize, a `plan` przeszedł przez `--via headless`.
- Zadania wielokrokowe z zależnościami i równoległością na prawdziwych agentach: przetestowano jedno zadanie.
- `codex`, `codex-acp` i `agy`: brak binarek w PATH, wszystkie checki SKIP. Komendy headless `codex exec`, `agy -p` i `opencode run` nie były uruchamiane.
- Kod wyjścia `cmd` przy nieznanym modelu (komunikat błędu zweryfikowany, kod nie).
- Koszt w USD: `cmd` go nie raportuje, więc tabele kosztów w README to szacunek z cennika modeli, bez rabatu cache.
- Regulamin: uruchamianie niezmodyfikowanego `claude` na własnym koncie jest zgodne z dokumentacją Anthropic według raportu research, ale strona Legal and compliance zmienia się często, więc sprawdź ją przed publikacją narzędzia.

## TODO

- Wspólne skille i MCP wstrzykiwane przez `session/new` do każdego agenta (mechanizm `mcp_servers` jest gotowy, brakuje konfiguracji).
- Sandbox Docker dla trybów YOLO.
- Kontynuacja pracy w tym samym worktree po uwagach recenzenta (teraz nowa próba startuje od czystego worktree z poprzednim patchem w prompcie).
- Koszt w USD liczony z cennika modeli (teraz tylko z danych agenta).
- Powtórzyć planowanie i recenzję przez Claude po podniesieniu limitu konta i dopisać zmierzony DAG do tabeli.
- Fragmenty strumienia odpowiedzi dzielące się na granicy odpytywania mogą zostać wypisane w osobnych liniach.
