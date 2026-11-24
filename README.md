# agent-orchestra

**Mocny model planuje i recenzuje, tanie modele implementują. Ty widzisz wszystko na żywo i możesz wtrącić się w każdej chwili.**

Orkiestrator agentów kodujących z hierarchią kosztową. Claude Code rozbija trudne zadanie na małe, testowalne części, a tani worker ([Command Code](https://commandcode.ai), model np. DeepSeek) wykonuje je równolegle w izolowanych git worktree. Zadanie trafia do wspólnej gałęzi dopiero po zielonym teście. Całość oglądasz w terminalowym interfejsie, w którym klikasz zamiast uczyć się komend.

![Przegląd TUI: tabela zadań, kroki workera na żywo, przyciski](docs/tui-overview.png)

## Po co

- **Taniej.** Planowanie, projektowanie i recenzja zostają przy mocnym modelu, a żmudna implementacja idzie do modelu około 20 razy tańszego za token.
- **Bezpieczniej.** Każdy worker pracuje w osobnym worktree, a `main` nie jest ruszany. Zmiany wracają na gałąź `orchestra/run-<id>`, którą recenzujesz i scalasz sam.
- **Widocznie.** Zadania, kroki workerów (komendy, zapisy plików, wyniki testów), tokeny i rozmowy agentów są w jednym oknie.
- **Sterowalnie.** Możesz dać workerowi wskazówkę, anulować przebieg, dodać nowego agenta, napisać do działającego agenta albo obejrzeć diff.

## Jak to działa

```mermaid
flowchart LR
    U([Ty]) --> C[Claude Code]
    C -- "run_plan (MCP)" --> S[Scheduler]
    S --> W1[worker w worktree A]
    S --> W2[worker w worktree B]
    W1 -- "test_cmd zielony" --> I[(gałąź orchestra/run-id)]
    W2 -- "test_cmd zielony" --> I
    I -- "diff do recenzji" --> C
    S -. zdarzenia .-> DB[(SQLite WAL)]
    DB -.-> T[TUI]
    T -. "wskazówki, anulowanie, wiadomości" .-> DB
```

1. Prosisz Claude'a o duże zadanie. Skill `orchestrate-hard-task` każe mu rozbić je na 3 do 8 zadań z opisem, plikami, kryteriami, komendą testową (`test_cmd`), katalogiem (`cwd`) i zależnościami.
2. `run_plan` startuje w tle. Zadania bez zależności idą równolegle (1 do 3), każde w świeżym worktree odgałęzionym od gałęzi integracyjnej.
3. Po zielonym `test_cmd` zadanie jest scalane (`--no-ff`) do gałęzi `orchestra/run-<id>`. Zadania zależne widzą kod swoich zależności.
4. Zadania, których worker nie rozwiązał, wracają do Claude'a. Diff całego przebiegu recenzuje Claude, a scala Ty.

## Szybki start

Wymagania: Python 3.11+, Node 22+, git, [Claude Code](https://claude.com/claude-code), `command-code` (`npm i -g command-code`). Opcjonalnie tmux (launcher `orch` sam otwiera w nim dwa panele).

```bash
git clone https://github.com/GrzegorzSzczepanek/agent-orchestra
cd agent-orchestra
scripts/setup.sh                 # .venv, instalacja, diagnostyka (pyta o npm i -g command-code)
cmd login                        # ręcznie: otwiera przeglądarkę (na serwerze: export COMMAND_CODE_API_KEY=...)
.venv/bin/orchestra doctor       # powinno być bez FAIL
```

Globalny launcher, który działa z dowolnego repozytorium git:

```bash
mkdir -p ~/.local/bin && ln -sf "$PWD/scripts/orch" ~/.local/bin/orch
cd ~/jakies/repo && orch
```

`orch` otwiera w jednym oknie Claude'a z podpiętym serwerem `orchestra` (lewy panel) oraz TUI (prawy panel). Nic nie trzeba rejestrować globalnie: serwer MCP jest podawany przez `--mcp-config`. Potem napisz do Claude'a, np.:

> Użyj orchestrate-hard-task. Dopisz testy jednostkowe do modułów w `src/`, które ich nie mają. Rozbij to na zadania i rozdaj workerom.

Model workera: `default_model` w `agents.toml` (domyślnie `deepseek/deepseek-v4.1-flash`), zmienna `ORCHESTRA_CMD_MODEL` ma pierwszeństwo. Lista modeli: `cmd --list-models`.

> **Node.** `cmd` i adapter ACP wymagają Node 22 lub nowszego. `orchestra`, `orchestra-mcp` i `orchestra-tui` same dopisują do PATH Node 22+ z nvm, gdy domyślny jest starszy albo `cmd` nie jest w PATH.

## Interfejs

Wszystko jest widoczne od razu, a każda akcja ma przycisk i F-klawisz (skróty tekstowe `/hint`, `/detail`, `/cancel`, `/msg` istnieją, ale nie są potrzebne).

| Przycisk | Klawisz | Co robi |
| --- | --- | --- |
| ＋ Agent | Ctrl+N | nowa sesja agenta dowolnego typu z `agents.toml`, we własnym worktree |
| ✉ Wiadomość | F3 | wiadomość do działającego agenta, dociera jako jego kolejny prompt |
| ✎ Wskazówka | F4 | tekst dołączany do następnej próby workera wybranego zadania |
| 🔎 Szczegóły | F5 | błąd, `test_cmd`, pełny wynik testów i pliki ostatniej próby |
| ⛔ Anuluj przebieg | F6 | zabija pracujące workery i kończy przebieg (z potwierdzeniem) |
| ± Diff | F2 | diff worktree zaznaczonego agenta |

Zaznacz wiersz po lewej, a pole tekstowe zmienia znaczenie: dla agenta to prompt, dla zadania to wskazówka (podpowiedź pod logiem mówi, co się stanie).

Gdy próba workera nie przechodzi testów, widać przyczynę, a nie tylko status:

![Nieudana próba z wynikiem testów](docs/tui-failure.png)

Rozmowy agentów i uwagi recenzenta są w wierszu `✉ wiadomości`. Nowego agenta dodajesz z listy, bez wpisywania komend:

![Rozmowy agentów i okno nowego agenta](docs/tui-new-agent.png)

Do samego podglądu zadań, bez promptów, jest osobny, lekki widok `orchestra watch` (albo `orch --simple-watch`):

![orchestra watch](docs/watch.png)

## Agenci rozmawiają ze sobą

Każdy agent startowany z TUI dostaje przez `session/new` podpięty serwer MCP `orchestra` z narzędziami `post_message`, `read_inbox` i `list_agents`. Wiadomość do działającego agenta (od Ciebie albo od innego agenta) trafia do niego jako kolejny prompt z instrukcją, jak odpowiedzieć. Po 10 wiadomościach agentów z rzędu bez Ciebie przekazywanie jest wstrzymywane, żeby rozmowa nie zapętliła się i nie paliła tokenów.

Workery z `run_plan` to jednorazowe wywołania `cmd -p`, więc nie rozmawiają w trakcie pracy. Mogą dostać wskazówkę przed następną próbą, a opcjonalna runda recenzji (`review_rounds`) odsyła do nich uwagi recenzenta.

## Narzędzia MCP

| Narzędzie | Opis |
| --- | --- |
| `run_plan(goal, tasks, parallel, review_rounds, ...)` | wykonuje plan zadań (DAG) workerami w tle, zwraca `run_id` od razu |
| `run_status(run_id, wait_s)` | stan zadań, po zakończeniu raport i gałąź `orchestra/run-<id>` do recenzji |
| `delegate(prompt, test_cmd, ...)` | pojedyncze proste zadanie w osobnym worktree z diffem i wynikiem testów |
| `post_message`, `read_inbox`, `list_agents` | skrzynka wiadomości między agentami |

Zadanie w `run_plan`: `id`, `description`, `files`, `acceptance`, `test_cmd` (komenda bez powłoki, np. `pytest -q tests/test_x.py`), `cwd` (podkatalog repo, w którym działa worker i test), `difficulty` (`easy`, `medium`, `hard`), `depends_on`. Workery startują z zacommitowanego `HEAD`, więc niezacommitowane zmiany są dla nich niewidoczne (launcher `orch` ostrzega o nich).

## Hierarchia kosztowa i zabezpieczenia

- **Bramka testów:** `test_cmd` musi przejść, zanim gałąź zadania zostanie scalona. Konflikt scalenia liczy się jak porażka.
- **Eskalacja:** po 2 kolejnych porażkach taniego workera zadanie dostaje jedną próbę od roli `planner` (w CLI `orchestra run`). W `run_plan` z Claude Code eskalacja jest wyłączona: nieudane zadanie wraca do Claude'a.
- **Budżet:** limit tokenów (domyślnie 400 tys.) i opcjonalnie kosztu na zadanie, liczone z logu SQLite.
- **Błędy krytyczne** (zła konfiguracja, brak logowania, brak kredytów) kończą zadanie bez ponawiania i zatrzymują resztę przebiegu.
- **Recenzent:** `review_rounds` 1 do 2 po zielonych testach. Badania nad debatą wieloagentową dają mieszane wyniki i wyższy koszt, więc domyślnie jest wyłączona.
- **Środowisko:** worker `cmd` dostaje `--yolo`, bo izolacją jest worktree. Nie uruchamiaj go na repozytorium z sekretami, których worker nie powinien widzieć. SDK ACP przekazuje agentom okrojone środowisko, sekrety trzeba wymienić jawnie w `env_passthrough`.

Realny koszt: pojedyncza próba workera w prawdziwych zadaniach zużywa rzędu 50 do 170 tys. tokenów wejścia (głównie kontekst i cache), a stały narzut `cmd -p` to około 15 tys. tokenów. Opłaca się to dla zadań z jasnym testem i 1 do 3 plikami, a nie dla jednolinijkowych poprawek.

## Konfiguracja i CLI

`agents.toml` opisuje agentów (komenda ACP, komenda headless, `env_passthrough`, model, poziom kosztowy) i role (`planner`, `reviewer`, `implementer`). Wbudowani: Claude Code (przez adapter ACP), Command Code, Codex, Antigravity, OpenCode.

```
orchestra doctor [--smoke] [--model ID]        diagnostyka: cmd, logowanie, modele, headless, ACP, inni agenci
orchestra plan "cel" [--out plan.json]         rola planner układa DAG zadań w JSON
orchestra run plan.json [--parallel 2] [--rounds 0] [--token-budget 400000]
orchestra watch [--repo .]                     podgląd przebiegów na żywo (tylko odczyt)
orchestra-tui --repo . --agents commandcode    TUI (--demo pokazuje agenty mock)
orchestra-mcp                                  serwer MCP (stdio)
orch [KATALOG] [--agents a,b] [--no-watch]     Claude Code + TUI w jednym oknie
scripts/go.sh "cel" [--claude] [--plan-only]   plan i wykonanie jednym poleceniem, bez Claude Code
```

## Struktura

```
agents.toml           agenci, role, model workera
.mcp.json             serwer MCP dla Claude Code uruchomionego w tym repo
.agents/skills/       skille (orchestrate-hard-task, delegate-simple-tasks), .claude/skills to symlink
src/orchestra/        scheduler, plan (DAG, pydantic), delegate, worktree, mcp_server, acp_client,
                      controller, store (SQLite WAL), progress, tui, watch, doctor, node_env, cli
scripts/              setup.sh, orch, go.sh, make_screenshot.py (zrzuty do README)
tests/                pytest, w tym zanonimizowane fixture'y z prawdziwego wyjścia cmd i claude
```

Stack: [Model Context Protocol](https://modelcontextprotocol.io) (FastMCP), [Agent Client Protocol](https://agentclientprotocol.com) (oficjalne SDK), Textual, pydantic, `graphlib`, SQLite.

## Testy i stan weryfikacji

```bash
.venv/bin/pytest        # 107 testów
```

**Zweryfikowane na prawdziwym środowisku** (macOS, Node 22, `cmd` 1.93.2, `claude` 2.1.x):
- `orchestra doctor`: logowanie, lista modeli, headless, ACP `initialize` i `session/new`.
- `run_plan` i `run_status` przez prawdziwy serwer MCP (stdio) z workerem `cmd`: 3 zadania, jedno zależne od dwóch pozostałych, wszystkie scalone, testy na gałęzi integracyjnej zielone.
- Podpięcie serwera `orchestra` do sesji `cmd acp`: agent sam wywołał `list_agents` i `post_message`.
- Planista Claude (`claude -p`) zwraca poprawny DAG, a worker go realizuje end-to-end.

**Zweryfikowane testami** (mocki i zanonimizowane nagrania): parser NDJSON `cmd` i `claude`, kody wyjścia, doctor, walidacja DAG-a, scheduler (równoległość, scalanie, bramka testów, eskalacja, budżet, konflikt scalenia, błąd krytyczny, anulowanie, wskazówki), `cwd` zadania, serwer MCP, klient ACP, TUI (przyciski, okna dialogowe, przekazywanie wiadomości z limitem).

**Niezweryfikowane:**
- Runda recenzji na prawdziwym Claude (`review_rounds`): tylko mocki.
- `codex`, `codex-acp` i `agy`: brak binarek, checki są pomijane.
- Koszt w USD workera: `cmd` go nie raportuje, więc kolumna `$` dla niego jest zerowa.
- Interaktywna sesja Claude Code wołająca `run_plan` na dużym, prawdziwym repozytorium.

## Ograniczenia i pomysły

- Workery `run_plan` nie mają podpiętego MCP, więc nie konsultują się w trakcie pracy (wymagałoby przejścia z `cmd -p` na sesje ACP).
- Anulowanie przebiegu zostawia worktree do posprzątania: `git worktree prune`, a gałęzie `orchestra/*` usuń po scaleniu.
- Brak ponawiania pojedynczego zadania z TUI (na razie wskazówka i ponowienie przez Claude'a).
- Koszt w USD liczony z cennika modeli, sandbox Docker dla trybu YOLO, kontynuacja pracy w tym samym worktree po uwagach recenzenta.

Uruchamianie niezmodyfikowanego `claude` na własnym koncie jest zgodne z dokumentacją Anthropic według autora, ale regulamin się zmienia, więc sprawdź aktualny przed użyciem w produkcji.
