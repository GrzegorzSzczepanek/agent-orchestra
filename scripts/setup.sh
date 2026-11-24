#!/usr/bin/env bash
# Instalacja środowiska agent-orchestra i diagnostyka. Użycie: scripts/setup.sh [--yes]
# --yes: odpowiada "tak" na pytanie o instalację command-code przez npm.
set -euo pipefail

cd "$(dirname "$0")/.."
ASSUME_YES=0
[[ "${1:-}" == "--yes" ]] && ASSUME_YES=1

say() { printf '\n== %s\n' "$*"; }

say "1/4 Środowisko Python (.venv)"
if command -v uv >/dev/null 2>&1; then
  [[ -d .venv ]] || uv venv .venv
  uv pip install -q -e ".[dev]" --python .venv/bin/python
else
  PY="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
  [[ -n "$PY" ]] || { echo "Brak python3 (wymagany >= 3.11)"; exit 1; }
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "Python >= 3.11 jest wymagany, znaleziono: $("$PY" -V)"; exit 1; }
  [[ -d .venv ]] || "$PY" -m venv .venv
  .venv/bin/python -m pip install -q -e ".[dev]"
fi
echo "OK: $(.venv/bin/orchestra --help >/dev/null 2>&1 && echo 'polecenie orchestra działa')"

say "2/4 Agenci (cmd, Claude Code i adapter ACP)"
if command -v cmd >/dev/null 2>&1; then
  echo "cmd znaleziony: $(cmd --version 2>/dev/null | head -1)"
else
  if ! command -v npm >/dev/null 2>&1; then
    echo "Brak npm. Zainstaluj Node.js, potem: npm i -g command-code"
  else
    ANSWER="n"
    if [[ $ASSUME_YES -eq 1 ]]; then
      ANSWER="t"
    elif [[ -t 0 ]]; then
      read -r -p "Zainstalować command-code przez 'npm i -g command-code'? [t/N] " ANSWER || true
    fi
    if [[ "$ANSWER" =~ ^[tTyY] ]]; then
      npm i -g command-code || {
        echo "Instalacja globalna nie powiodła się (uprawnienia?). Ustaw prefiks bez sudo:"
        echo "  npm config set prefix ~/.local && export PATH=\"\$HOME/.local/bin:\$PATH\" && npm i -g command-code"
      }
    else
      echo "Pominięto. Zainstaluj później: npm i -g command-code"
    fi
  fi
fi

if command -v claude >/dev/null 2>&1 && command -v claude-agent-acp >/dev/null 2>&1; then
  echo "Claude Code: $(claude --version 2>/dev/null | head -1), adapter ACP: $(claude-agent-acp --version 2>/dev/null | head -1)"
else
  echo "Brak Claude Code lub adaptera ACP. Pod tym samym Node (adapter wymaga >=22):"
  echo "  npm i -g @anthropic-ai/claude-code @agentclientprotocol/claude-agent-acp"
  echo "nvm trzyma globalne paczki osobno dla każdej wersji Node, więc po nvm use zainstaluj ponownie."
fi

say "3/4 Kroki ręczne (wymagają Ciebie)"
cat <<'MSG'
  a) Logowanie:  cmd login        (albo na serwerze bez przeglądarki: export COMMAND_CODE_API_KEY=...)
  b) Model:      cmd --list-models
                 export ORCHESTRA_CMD_MODEL=deepseek/deepseek-v4.1-flash   # ID z listy
  c) Test:       .venv/bin/orchestra doctor --smoke     # zużywa kilka groszy kredytów
MSG

say "4/4 Diagnostyka (bez zużycia kredytów)"
.venv/bin/orchestra doctor || true
echo
echo "Wklej zawartość orchestra-doctor.json do rozmowy (raport nie zawiera sekretów)."
