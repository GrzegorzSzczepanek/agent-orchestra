#!/usr/bin/env bash
# Install the agent-orchestra environment and run diagnostics. Usage: scripts/setup.sh [--yes]
# --yes: answers "yes" to the question about installing command-code through npm.
set -euo pipefail

cd "$(dirname "$0")/.."
ASSUME_YES=0
[[ "${1:-}" == "--yes" ]] && ASSUME_YES=1

say() { printf '\n== %s\n' "$*"; }

say "1/4 Python environment (.venv)"
if command -v uv >/dev/null 2>&1; then
  [[ -d .venv ]] || uv venv .venv
  uv pip install -q -e ".[dev]" --python .venv/bin/python
else
  PY="$(command -v python3.13 || command -v python3.12 || command -v python3.11 || command -v python3 || true)"
  [[ -n "$PY" ]] || { echo "No python3 found (>= 3.11 required)"; exit 1; }
  "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
    || { echo "Python >= 3.11 is required, found: $("$PY" -V)"; exit 1; }
  [[ -d .venv ]] || "$PY" -m venv .venv
  .venv/bin/python -m pip install -q -e ".[dev]"
fi
echo "OK: $(.venv/bin/orchestra --help >/dev/null 2>&1 && echo 'orchestra command works')"

say "2/4 Agents (cmd, Claude Code and the ACP adapter)"
if command -v cmd >/dev/null 2>&1; then
  echo "cmd found: $(cmd --version 2>/dev/null | head -1)"
else
  if ! command -v npm >/dev/null 2>&1; then
    echo "No npm found. Install Node.js, then: npm i -g command-code"
  else
    ANSWER="n"
    if [[ $ASSUME_YES -eq 1 ]]; then
      ANSWER="y"
    elif [[ -t 0 ]]; then
      read -r -p "Install command-code with 'npm i -g command-code'? [y/N] " ANSWER || true
    fi
    if [[ "$ANSWER" =~ ^[tTyY] ]]; then
      npm i -g command-code || {
        echo "Global install failed (permissions?). Set a prefix without sudo:"
        echo "  npm config set prefix ~/.local && export PATH=\"\$HOME/.local/bin:\$PATH\" && npm i -g command-code"
      }
    else
      echo "Skipped. Install later: npm i -g command-code"
    fi
  fi
fi

if command -v claude >/dev/null 2>&1 && command -v claude-agent-acp >/dev/null 2>&1; then
  echo "Claude Code: $(claude --version 2>/dev/null | head -1), ACP adapter: $(claude-agent-acp --version 2>/dev/null | head -1)"
else
  echo "No Claude Code or ACP adapter. Under the same Node (the adapter requires >=22):"
  echo "  npm i -g @anthropic-ai/claude-code @agentclientprotocol/claude-agent-acp"
  echo "nvm keeps global packages separately for each Node version, so reinstall after nvm use."
fi

say "3/4 Manual steps (need you)"
cat <<'MSG'
  a) Login:   cmd login        (or on a browserless server: export COMMAND_CODE_API_KEY=...)
  b) Model:   cmd --list-models
              export ORCHESTRA_CMD_MODEL=deepseek/deepseek-v4.1-flash   # ID from the list
  c) Test:    .venv/bin/orchestra doctor --smoke     # spends a few cents of credits
MSG

say "4/4 Diagnostics (no credits spent)"
.venv/bin/orchestra doctor || true
echo
echo "Paste the contents of orchestra-doctor.json into the chat (the report contains no secrets)."
