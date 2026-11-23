---
name: delegate-simple-tasks
description: Użyj, gdy zadanie implementacyjne ma jasny zakres, znane pliki i komendę testową. Deleguje je tańszemu workerowi przez narzędzie MCP delegate, a diff recenzuje mocny model.
---

# Delegowanie prostych zadań

1. Oceń zadanie. Delegujesz tylko wtedy, gdy znasz pliki, kryteria akceptacji i komendę testową.
2. Przygotuj `task_spec`: `prompt` (konkretny, z nazwami plików), `test_cmd`, opcjonalnie `model`.
3. Wywołaj narzędzie `delegate`. Dostaniesz diff, wynik testów i zużycie tokenów.
4. Sprawdź `status`. `ok` oznacza zielone testy. `config_error` (brak modelu: ustaw ORCHESTRA_CMD_MODEL), `auth_failed`, `insufficient_credits`, `worker_not_found` to problemy środowiska, nie ponawiaj ich, powiedz o nich użytkownikowi. `max_turns` i `tests_failed` ponów najwyżej raz z doprecyzowanym promptem.
5. Zrecenzuj diff, a nie całą sesję workera. Przy dwóch porażkach testów z rzędu wykonaj zadanie sam.
6. Nie deleguj zmian w schematach, bezpieczeństwie ani zadań wymagających researchu.
