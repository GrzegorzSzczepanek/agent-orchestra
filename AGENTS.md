# AGENTS.md

Repozytorium: agent-orchestra. Orkiestrator agentów kodujących z hierarchią kosztową.

## Zasady dla agentów
- Najpierw szukaj gotowych bibliotek, potem pisz własny kod. Przed użyciem API sprawdź aktualną dokumentację i zainstalowaną wersję.
- Python 3.11+, src layout, testy w `tests/` (pytest). Uruchom `.venv/bin/pytest` przed zakończeniem pracy.
- Zadania proste (jasny zakres, znana komenda testowa) deleguj tanim workerom przez narzędzie MCP `delegate`. Planowanie, research i recenzję diffów zostaw mocnemu modelowi.
- Skille: katalog `.agents/skills/` jest źródłem prawdy, `.claude/skills` to symlink.
- README po polsku, bez myślników em/en jako łączników wyjaśnień.
- Commituj małymi krokami z opisowym komunikatem.
