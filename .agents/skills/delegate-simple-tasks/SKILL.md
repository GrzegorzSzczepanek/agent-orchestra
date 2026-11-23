---
name: delegate-simple-tasks
description: Użyj, gdy zadanie implementacyjne ma jasny zakres, znane pliki i komendę testową. Deleguje je tańszemu workerowi przez narzędzie MCP delegate, a diff recenzuje mocny model.
---

# Delegowanie prostych zadań

1. Oceń zadanie. Delegujesz tylko wtedy, gdy znasz pliki, kryteria akceptacji i komendę testową.
2. Przygotuj `task_spec`: `prompt` (konkretny, z nazwami plików), `test_cmd`, opcjonalnie `model`.
3. Wywołaj narzędzie `delegate`. Dostaniesz diff, wynik testów i zużycie tokenów.
4. Zrecenzuj diff, a nie całą sesję workera. Przy dwóch porażkach testów z rzędu wykonaj zadanie sam.
5. Nie deleguj zmian w schematach, bezpieczeństwie ani zadań wymagających researchu.
