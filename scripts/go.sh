#!/usr/bin/env bash
# Jeden skrypt: plan i wykonanie zadania przez orkiestratora.
# Uzycie: scripts/go.sh "cel zadania" [--repo KATALOG] [--parallel N] [--claude] [--plan-only] [--tui] [--watch]
# Domyslnie planner to commandcode (dziala bez limitu Claude). --claude uzywa agents.toml z Claude.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
ORCH="$ROOT/.venv/bin/orchestra"
TUI_BIN="$ROOT/.venv/bin/orchestra-tui"
GOAL=""
REPO="$PWD"
PARALLEL=2
PLANNER=commandcode
PLAN_ONLY=0
TUI=0
WATCH=0
CONFIG=""

usage() {
  sed -n '2,4p' "$0" | sed 's/^# \{0,1\}//'
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="${2:?brak wartosci dla --repo}"; shift 2 ;;
    --parallel) PARALLEL="${2:?brak wartosci dla --parallel}"; shift 2 ;;
    --config) CONFIG="${2:?brak wartosci dla --config}"; shift 2 ;;
    --claude) PLANNER=claude; shift ;;
    --plan-only) PLAN_ONLY=1; shift ;;
    --tui) TUI=1; shift ;;
    --watch) WATCH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "Nieznana opcja: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -n "$GOAL" ]]; then
        echo "Podaj tylko jeden cel zadania." >&2
        exit 2
      fi
      GOAL="$1"; shift ;;
  esac
done

[[ -x "$ORCH" ]] || { echo "Brak .venv. Uruchom: scripts/setup.sh" >&2; exit 1; }
[[ -d "$REPO/.git" ]] || { echo "To nie jest repozytorium git: $REPO" >&2; exit 1; }
REPO="$(cd "$REPO" && pwd)"

if [[ -z "$(cmd status 2>/dev/null | grep -iE 'authenticated|zalogowan' || true)" ]]; then
  echo "Uwaga: cmd wyglada na niezalogowany. Uruchom: cmd login" >&2
fi

if [[ -z "$CONFIG" ]]; then
  if [[ "$PLANNER" == claude ]]; then
    CONFIG="$ROOT/agents.toml"
  else
    CONFIG="$(mktemp "${TMPDIR:-/tmp}/orchestra-agents.XXXXXX")"
    trap 'rm -f "$CONFIG"' EXIT
    sed 's/^planner = "claude"/planner = "commandcode"/; s/^reviewer = "claude"/reviewer = "commandcode"/' \
      "$ROOT/agents.toml" > "$CONFIG"
  fi
fi

if [[ $TUI -eq 1 ]]; then
  AGENTS="commandcode"
  [[ "$PLANNER" == claude ]] && AGENTS="claude"
  exec "$TUI_BIN" --repo "$REPO" --config "$CONFIG" --agents "$AGENTS"
fi

[[ -n "$GOAL" ]] || { echo "Podaj cel zadania, np.: scripts/go.sh \"dodaj funkcje median\"" >&2; usage >&2; exit 2; }

PLAN="$REPO/.orchestra/plan.json"
mkdir -p "$REPO/.orchestra"

echo "== Plan ($PLANNER)"
"$ORCH" plan "$GOAL" --config "$CONFIG" --repo "$REPO" --out "$PLAN"
[[ $PLAN_ONLY -eq 1 ]] && exit 0

echo
echo "== Wykonanie"
if [[ $WATCH -eq 1 ]]; then
  echo "Podglad na zywo w drugim terminalu: $ORCH watch --repo $REPO"
fi
"$ORCH" run "$PLAN" --config "$CONFIG" --repo "$REPO" --parallel "$PARALLEL"
