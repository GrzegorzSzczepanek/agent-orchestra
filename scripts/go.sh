#!/usr/bin/env bash
# Plan and run one task through the orchestrator.
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
  cat <<'EOF'
Plan and run one task through the orchestrator.

Usage:
  scripts/go.sh "task goal" [options]

Options:
  --repo DIR      Git repository to work in (default: current directory).
  --parallel N    Number of tasks to run at once (default: 2).
  --config FILE   Agent config file to use. Default: agents.toml with planner
                  and reviewer switched to commandcode unless --claude is given.
  --claude        Use agents.toml as is, with Claude as planner and reviewer.
  --plan-only     Write the plan to .orchestra/plan.json and stop.
  --tui           Open the TUI instead of planning and running.
  --watch         Print a live watch command for a second terminal.
  -h, --help      Show this help and exit.

Examples:
  scripts/go.sh "add a median function"
  scripts/go.sh "refactor the parser" --repo ~/src/project --parallel 4
  scripts/go.sh "split the CLI" --claude --plan-only
  scripts/go.sh --tui
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --repo) REPO="${2:?missing value for --repo}"; shift 2 ;;
    --parallel) PARALLEL="${2:?missing value for --parallel}"; shift 2 ;;
    --config) CONFIG="${2:?missing value for --config}"; shift 2 ;;
    --claude) PLANNER=claude; shift ;;
    --plan-only) PLAN_ONLY=1; shift ;;
    --tui) TUI=1; shift ;;
    --watch) WATCH=1; shift ;;
    -h|--help) usage; exit 0 ;;
    -*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    *)
      if [[ -n "$GOAL" ]]; then
        echo "Provide only one task goal." >&2
        exit 2
      fi
      GOAL="$1"; shift ;;
  esac
done

[[ -x "$ORCH" ]] || { echo "No .venv found. Run: scripts/setup.sh" >&2; exit 1; }
[[ -d "$REPO/.git" ]] || { echo "Not a git repository: $REPO" >&2; exit 1; }
REPO="$(cd "$REPO" && pwd)"

if [[ -z "$(cmd status 2>/dev/null | grep -iE 'authenticated|zalogowan' || true)" ]]; then
  echo "Warning: cmd does not look logged in. Run: cmd login" >&2
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

[[ -n "$GOAL" ]] || { echo "Provide a task goal, for example: scripts/go.sh \"add a median function\"" >&2; usage >&2; exit 2; }

PLAN="$REPO/.orchestra/plan.json"
mkdir -p "$REPO/.orchestra"

echo "== Plan ($PLANNER)"
"$ORCH" plan "$GOAL" --config "$CONFIG" --repo "$REPO" --out "$PLAN"
[[ $PLAN_ONLY -eq 1 ]] && exit 0

echo
echo "== Run"
if [[ $WATCH -eq 1 ]]; then
  echo "Live watch in another terminal: $ORCH watch --repo $REPO"
fi
"$ORCH" run "$PLAN" --config "$CONFIG" --repo "$REPO" --parallel "$PARALLEL"
