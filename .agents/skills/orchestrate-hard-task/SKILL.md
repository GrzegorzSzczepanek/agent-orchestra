---
name: orchestrate-hard-task
description: Użyj przy dużym lub trudnym zadaniu implementacyjnym, które da się rozbić na kilka małych, testowalnych części. Model sterujący (Claude, OpenCode, Codex) planuje i recenzuje, a części wykonują tanie workery przez narzędzia MCP run_plan i run_status.
---

# Rozbijanie trudnego zadania i rozdawanie workerom

1. **Zbadaj repo** (grep, istniejące testy). Ustal, jak uruchamia się testy. Pamiętaj: workery widzą tylko zacommitowany HEAD, więc najpierw zacommituj lub zastash'uj zmiany, od których zależy zadanie.
2. **Rozbij cel na 3 do 8 zadań.** Każde ma: `id`, samowystarczalny `description` (nazwy plików, sygnatury, oczekiwane zachowanie), `files`, `acceptance`, `test_cmd` (jedna komenda bez powłoki, np. `pytest -q tests/test_x.py`, test ma failować przed zmianą). Komenda odpala się w czystym worktree z HEAD, bez `.venv` i innych plików spoza gita, więc nie używaj ścieżek względnych do nich (`.venv/bin/pytest`). Podaj polecenie z PATH (`python3 -m pytest -q ...`) albo pełną ścieżkę do binarki. Gdy programu nie ma, zadanie kończy się `config_error` od razu, bez zużycia tokenów, `cwd` (podkatalog repo, w którym działa worker i test, gdy projekt nie jest w korzeniu, np. `code`; `test_cmd` odpala się właśnie tam), `difficulty`, `depends_on`. Użytkownik widzi pracę workerów na żywo i może wysyłać im wskazówki (`/hint`) albo anulować przebieg (`/cancel`). Zadania w różnych plikach bez zależności idą równolegle. Jedno zadanie nie powinno dotykać więcej niż 1 do 3 plików.
3. **Zadania `hard`, projektowe, bezpieczeństwo i wszystko bez sprawdzalnego testu zrób sam**, nie wkładaj ich do planu. Jeśli zadanie zależy od Twojej części, zacommituj ją przed `run_plan`.
4. Wywołaj `run_plan(goal, tasks)` (opcjonalnie `review_rounds=1`, gdy chcesz niezależnej oceny diffu każdego zadania przez rolę reviewer, kosztem jej limitu). Zwraca `run_id` od razu. Powiedz użytkownikowi, że postęp widać w panelu TUI obok (uruchomionym przez `orch`). Bez TUI: `orchestra watch --repo .`.
5. Pytaj `run_status(run_id, wait_s=60)` aż `finished=true`. Przy `config_error`, `auth_failed`, `insufficient_credits` nie ponawiaj, powiedz użytkownikowi.
6. **Zrecenzuj wynik:** `git diff HEAD...<integration_branch>` (gałąź z `integration_branch`). Zadania ze statusem `failed`, `blocked` albo `over_budget` zrób sam. Rozwiąż problemy, potem scal: `git merge --no-ff <integration_branch>`.
7. Uruchom pełne testy projektu przed zakończeniem i pokaż wynik.

Pojedyncze, proste zadanie bez planu: skill `delegate-simple-tasks` (narzędzie `delegate`).

Użytkownik widzi tabelę zadań w TUI i może wysyłać wskazówki workerom albo wiadomości działającym agentom. Agentów z TUI możesz zobaczyć przez `list_agents` i napisać do nich przez `post_message`.
