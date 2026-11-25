"""`orchestra` entry point: doctor, plan, run and friends."""

from __future__ import annotations

import argparse
import asyncio
import shutil
import sys
from pathlib import Path

from .config import load_config
from .worktree import ensure_local_exclude


class _HelpFormatter(argparse.RawDescriptionHelpFormatter):
    """Keeps hand written descriptions and examples as they are; width follows the terminal, at most 98."""

    def __init__(self, prog: str):
        super().__init__(prog, max_help_position=30, width=min(shutil.get_terminal_size().columns - 2, 98))


MAIN_DESCRIPTION = """\
Cost-aware orchestrator for coding agents.
A strong model plans and reviews, cheap workers implement. Every task runs in its own git
worktree and is merged into an integration branch only when its test command passes.

Quick start:
  1. orchestra doctor                  check tools, logins and models
  2. orchestra plan "add a median()"   the planner writes plan.json (or: orchestra init-plan)
  3. orchestra run plan.json           workers implement, tests gate every task
  4. git merge --no-ff BRANCH          after you reviewed the integration branch"""

MAIN_EPILOG = """\
Examples:
  orchestra doctor
  orchestra plan "add input validation" --repo ~/src/project
  orchestra run plan.json --parallel 3 --rounds 1
  orchestra watch --repo .
  orchestra roles --set planner=opencode

Run `orchestra <command> --help` for the options and examples of one command.
Interactive terminal UI: `orch` (launcher) or `orchestra-tui`."""


def _add_command(sub, name: str, summary: str, description: str, *examples: str) -> argparse.ArgumentParser:
    return sub.add_parser(name, help=summary, description=description, formatter_class=_HelpFormatter,
                          epilog="Examples:\n" + "\n".join(f"  {e}" for e in examples))


def _add_config(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default="agents.toml", help="path to agents.toml (default: ./agents.toml)")


def _add_repo(p: argparse.ArgumentParser) -> None:
    p.add_argument("--repo", default=".", help="git repository to work in (default: current directory)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="orchestra", description=MAIN_DESCRIPTION, epilog=MAIN_EPILOG,
                                 formatter_class=_HelpFormatter)
    sub = ap.add_subparsers(dest="command", title="commands", metavar="<command>")

    d = _add_command(sub, "doctor", "check tools, logins, models and agents",
                     "Diagnose the setup: cmd binary, login, model list, headless mode, ACP and other agents.\n"
                     "Prints a PASS/FAIL report and saves it as JSON without secrets.",
                     "orchestra doctor",
                     "orchestra doctor --smoke --model deepseek/deepseek-v4.1-flash",
                     "orchestra doctor --no-others --json report.json")
    _add_config(d)
    d.add_argument("--smoke", action="store_true",
                   help="run a real headless test (cmd -p), uses a small amount of credits")
    d.add_argument("--model", default="", help="model ID to check (default: from agents.toml or its env variable)")
    d.add_argument("--timeout", type=float, default=30, help="time limit of one probe in seconds (default: 30)")
    d.add_argument("--no-others", action="store_true", help="skip agents other than commandcode")
    d.add_argument("--json", default="orchestra-doctor.json", help="JSON report path (default: %(default)s)")

    pl = _add_command(sub, "plan", "let the planner turn a goal into plan.json",
                      "The planner role splits a goal into a dependency graph of small, testable tasks.",
                      'orchestra plan "add a median function"',
                      'orchestra plan "split the parser" --out parser.json --via acp')
    pl.add_argument("goal", help="what you want to achieve, in one sentence")
    _add_config(pl)
    _add_repo(pl)
    pl.add_argument("--out", default="plan.json", help="where to save the plan (default: %(default)s)")
    pl.add_argument("--via", choices=["headless", "acp"], default="headless",
                    help="how to talk to the planner (default: %(default)s)")
    pl.add_argument("--model", default="", help="planner model (default: from the agent configuration)")

    ip = _add_command(sub, "init-plan", "write a plan template to fill in by hand",
                      "Write a JSON plan template so you can split the work yourself, then use `orchestra run`.",
                      "orchestra init-plan",
                      "orchestra init-plan my-plan.json")
    ip.add_argument("out", nargs="?", default="plan.json", help="file to create (default: %(default)s)")

    r = _add_command(sub, "run", "execute a plan with cheap workers",
                     "Execute a plan: one worktree per task, test gate, retries, escalation and merge into\n"
                     "an integration branch. Workers only see committed changes (HEAD).",
                     "orchestra run plan.json",
                     "orchestra run plan.json --parallel 3 --rounds 1",
                     'orchestra run plan.json --integration-test "python3 -m pytest -q" --no-escalate')
    r.add_argument("plan", help="plan JSON file")
    _add_config(r)
    _add_repo(r)
    r.add_argument("--report", default="", help="JSON report path (default: .orchestra/runs/<id>.json)")
    workers = r.add_argument_group("workers")
    workers.add_argument("--worker", default="", help="override the agent of the implementer role")
    workers.add_argument("--model", default="", help="cheap worker model (default: variable from agents.toml)")
    workers.add_argument("--parallel", type=int, default=2, help="tasks at once, 1 to 3 (default: 2)")
    workers.add_argument("--timeout", type=float, default=900,
                         help="time limit of one worker in seconds (default: 900)")
    gates = r.add_argument_group("quality gates")
    gates.add_argument("--rounds", type=int, default=0, help="review rounds after green tests, 0 to 2 (default: 0)")
    gates.add_argument("--integration-test", default="", metavar="CMD",
                       help="full tests after every merge (a failing merge is reverted)")
    gates.add_argument("--no-red-check", action="store_true",
                       help="do not require test_cmd to fail before the worker's changes")
    limits = r.add_argument_group("failures and budgets")
    limits.add_argument("--max-failures", type=int, default=2,
                        help="cheap worker failures in a row before escalation (default: 2)")
    limits.add_argument("--no-escalate", action="store_true", help="never escalate to the planner role")
    limits.add_argument("--token-budget", type=int, default=400_000,
                        help="tokens per task, 0 disables the limit (default: 400000)")
    limits.add_argument("--cost-budget", type=float, default=0.0,
                        help="USD per task, 0 disables the limit (default: 0)")

    w = _add_command(sub, "watch", "live view of runs (read only)",
                     "Live, read only view of run_plan runs and delegations. Keys: n next run, q quit.",
                     "orchestra watch",
                     "orchestra watch --repo ~/src/project")
    w.add_argument("--repo", default=".", help="repository whose event log you watch (default: current directory)")
    w.add_argument("--db", default=None, help="event database (default: <repo>/.orchestra/events.db)")

    ro = _add_command(sub, "roles", "show or change who plans, reviews and implements",
                      "Show which agent plays the planner, reviewer and implementer roles (with fallback),\n"
                      "or change that choice and an agent's model.",
                      "orchestra roles",
                      "orchestra roles --set planner=opencode --set reviewer=claude",
                      "orchestra roles --model opencode=opencode/big-pickle",
                      "orchestra roles --set planner=    # clear the choice")
    _add_config(ro)
    _add_repo(ro)
    ro.add_argument("--set", action="append", default=[], metavar="ROLE=AGENT",
                    help="choose the agent of a role (an empty value clears the choice)")
    ro.add_argument("--model", action="append", default=[], metavar="AGENT=MODEL",
                    help="set the model of an agent (an empty value clears it)")

    lg = _add_command(sub, "ledger", "delegation ledger and worker budget",
                      "Track whether delegating pays off and cap the estimated cost of worker sessions.",
                      "orchestra ledger budget --set 1.0",
                      "orchestra ledger add RUN TASK --outcome accepted",
                      "orchestra ledger show")
    lsub = lg.add_subparsers(dest="ledger_cmd", required=True, title="actions", metavar="<action>",
                             help="one of the actions below")
    la = _add_command(lsub, "add", "record the review of a delegated task",
                      "Record how a delegated task turned out, with estimated strong model tokens.",
                      "orchestra ledger add 0ed4e12f core --outcome accepted",
                      "orchestra ledger add 0ed4e12f tui --outcome fixed --spec-file spec.md")
    ls = _add_command(lsub, "show", "delegation table and break-even threshold",
                      "Show recorded delegations, the proposed break-even threshold and the budget.",
                      "orchestra ledger show")
    lb = _add_command(lsub, "budget", "show or set the worker session budget",
                      "Show or start a session budget in USD (estimated from the prices in agents.toml).",
                      "orchestra ledger budget",
                      "orchestra ledger budget --set 1.0",
                      "orchestra ledger budget --check")
    for p in (la, ls, lb):
        _add_config(p)
        _add_repo(p)
    la.add_argument("run_id", help="run ID printed by `orchestra run` or run_plan")
    la.add_argument("task_id", help="task ID from the plan")
    la.add_argument("--outcome", required=True, choices=["accepted", "fixed", "rejected"],
                    help="accepted: no changes, fixed: needed fixes, rejected: redone by hand")
    la.add_argument("--spec-file", action="append", default=[],
                    help="file with the description or test written by the strong model (repeatable)")
    la.add_argument("--diff-branch", default="", help="branch with the worker's result (diff size = review cost)")
    la.add_argument("--notes-chars", type=int, default=0, help="number of characters of review notes")
    la.add_argument("--note", default="", help="free text stored with the entry")
    lb.add_argument("--set", type=float, default=None, metavar="USD",
                    help="start a new session with a USD limit (0 disables it)")
    lb.add_argument("--check", action="store_true", help="exit with code 3 when the budget is exceeded")
    return ap


def cmd_plan(args: argparse.Namespace) -> int:
    from .agents_io import acp_ask, headless_ask, repo_files
    from .plan import PlanError, make_plan
    from .store import EventStore
    from .worktree import ensure_local_exclude

    repo = Path(args.repo).resolve()
    ensure_local_exclude(repo)
    cfg = load_config(args.config, repo=repo)
    store = EventStore(repo / ".orchestra" / "events.db")
    ask = (acp_ask(cfg, "planner", repo, store) if args.via == "acp"
           else headless_ask(cfg, "planner", repo, model=args.model, store=store))

    async def go():
        return await make_plan(args.goal, ask, repo_files=await repo_files(repo))

    try:
        plan = asyncio.run(go())
    except (PlanError, RuntimeError) as exc:
        print(f"Planning error: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"Plan: {len(plan.tasks)} tasks -> {Path(args.out).resolve()}")
    for t in plan.tasks:
        dep = f" (after: {', '.join(t.depends_on)})" if t.depends_on else ""
        print(f"  {t.id} [{t.difficulty}]{dep}: {t.description[:70]}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    import json

    from .agents_io import headless_ask, make_reviewer
    from .plan import Plan
    from .scheduler import RunOptions, run_plan
    from .store import EventStore
    from .worktree import ensure_local_exclude

    repo = Path(args.repo).resolve()
    ensure_local_exclude(repo)
    cfg = load_config(args.config, repo=repo)
    plan = Plan.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
    store = EventStore(repo / ".orchestra" / "events.db")
    opts = RunOptions(parallel=args.parallel, max_failures=args.max_failures, escalate=not args.no_escalate,
                      token_budget=args.token_budget, cost_budget_usd=args.cost_budget,
                      discussion_rounds=args.rounds, worker=args.worker, model=args.model, timeout_s=args.timeout,
                      integration_test_cmd=args.integration_test, red_check=not args.no_red_check)
    reviewer = (make_reviewer(headless_ask(cfg, "reviewer", repo, store=store))
                if opts.discussion_rounds else None)
    report = asyncio.run(run_plan(plan, cfg, repo, store, opts, reviewer=reviewer))
    print(report.render())
    out = Path(args.report) if args.report else repo / ".orchestra" / "runs" / f"{report.run_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nReport: {out}\nReview and merge: git log --oneline {report.integration_branch}")
    return 0 if report.ok else 1


PLAN_TEMPLATE = {
    "goal": "The whole goal in one sentence",
    "tasks": [
        {"id": "module-a", "description": "Self-contained description: files, signatures, expected behaviour.",
         "files": ["src/module_a.py", "tests/test_module_a.py"], "acceptance": ["test_cmd passes"],
         "test_cmd": "pytest -q tests/test_module_a.py", "cwd": "", "difficulty": "easy", "depends_on": []},
        {"id": "module-b", "description": "Task that depends on module-a (starts after it is merged).",
         "files": ["src/module_b.py", "tests/test_module_b.py"], "acceptance": ["test_cmd passes"],
         "test_cmd": "pytest -q tests/test_module_b.py", "cwd": "", "difficulty": "medium", "depends_on": ["module-a"]},
    ],
}


def cmd_init_plan(args: argparse.Namespace) -> int:
    import json

    from .plan import Plan

    out = Path(args.out)
    if out.exists():
        print(f"{out} already exists, nothing was overwritten.", file=sys.stderr)
        return 1
    Plan.model_validate(PLAN_TEMPLATE)  # the template must always be valid
    out.write_text(json.dumps(PLAN_TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Plan template: {out.resolve()}\nEdit it and run: orchestra run {out} --repo .  (or the Plan button in the TUI)")
    return 0


def cmd_roles(args: argparse.Namespace) -> int:
    from .config import save_overrides

    repo = Path(args.repo).resolve()
    cfg = load_config(args.config, repo=repo)
    try:
        roles = dict(item.split("=", 1) for item in args.set)
        models = dict(item.split("=", 1) for item in args.model)
    except ValueError:
        print("Usage: --set role=agent, --model agent=model", file=sys.stderr)
        return 2
    bad = [r for r in roles if r not in ("planner", "reviewer", "implementer")] + \
          [a for a in [*roles.values(), *models] if a and a not in cfg.agents]
    if bad:
        print(f"Unknown: {', '.join(bad)}. Agents: {', '.join(cfg.agents)}", file=sys.stderr)
        return 2
    if roles or models:
        ensure_local_exclude(repo)
        save_overrides(repo, roles=roles, models=models)
        cfg.reload_overrides(repo)
    for role in ("planner", "reviewer", "implementer"):
        info = cfg.role_info(role)
        note = f"  ({info.reason}; in agents.toml: {info.configured})" if info.changed else ""
        print(f"{role:12} {info.effective}{note}")
    print("\nAgents:")
    for name, agent in cfg.agents.items():
        model = agent.resolve_model() or "-"
        print(f"  {name:12} {'installed' if agent.is_available() else 'not in PATH':14} {agent.tier:7} model: {model}")
    return 0


def cmd_ledger(args: argparse.Namespace) -> int:
    from . import ledger
    from .ledger_report import format_table, proposed_threshold
    from .store import EventStore
    from .worktree import ensure_local_exclude

    repo = Path(args.repo).resolve()
    ensure_local_exclude(repo)
    cfg = load_config(args.config, repo=repo)
    store = EventStore(repo / ".orchestra" / "events.db")
    if args.ledger_cmd == "add":
        import subprocess

        worker, tin, tout, usd = ledger.task_worker_usage(store, cfg, args.run_id, args.task_id)
        if not worker:
            print(f"No attempts of task {args.task_id} in run {args.run_id}.", file=sys.stderr)
            return 1
        spec = sum(ledger.estimate_tokens(Path(f).read_text(encoding="utf-8")) for f in args.spec_file)
        diff_tokens = 0
        if args.diff_branch:
            out = subprocess.run(["git", "diff", f"HEAD...{args.diff_branch}"], cwd=repo, capture_output=True, text=True)
            diff_tokens = ledger.estimate_tokens(out.stdout) if out.returncode == 0 else 0
        review = diff_tokens + ledger.estimate_tokens("x" * args.notes_chars)
        store.add_delegation(run_id=args.run_id, task_id=args.task_id, outcome=args.outcome, worker=worker,
                             worker_tokens_in=tin, worker_tokens_out=tout, worker_usd=usd, spec_tokens=spec,
                             review_tokens=review, diff_tokens=diff_tokens, note=args.note)
        print(f"Recorded {args.task_id}: worker {tin}/{tout} tok (~{usd:.4f} USD), spec ~{spec}, review ~{review}, "
              f"diff ~{diff_tokens} (strong model tokens are an estimate: characters / {ledger.CHARS_PER_TOKEN})")
        return 0
    if args.ledger_cmd == "show":
        rows = ledger.rows_from_store(store)
        print(format_table(rows))
        print("\n" + proposed_threshold(rows).text)
    if args.ledger_cmd in ("show", "budget"):
        if args.ledger_cmd == "budget" and args.set is not None:
            ledger.start_session(repo, args.set)
        state = ledger.budget_state(repo, store, cfg)
        if state is None:
            print("Session budget: not set (orchestra ledger budget --set USD).")
            return 0
        flag = "  EXCEEDED" if state.exceeded else ""
        print(f"Session budget: {state.spent_usd:.4f} of {state.limit_usd:.2f} USD (estimate from prices in agents.toml){flag}")
        if state.unpriced_agents:
            print(f"Warning: no price in agents.toml for: {', '.join(state.unpriced_agents)} (counted as 0).")
        if args.ledger_cmd == "budget" and args.check and state.exceeded:
            return 3
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    from .watch import main as watch_main

    watch_main(args.repo, args.db)
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    cfg = load_config(args.config)
    rep = asyncio.run(run_doctor(cfg, smoke=args.smoke, model=args.model, timeout=args.timeout,
                                 others=not args.no_others))
    print(rep.render())
    rep.save(args.json)
    print(f"\nJSON report: {Path(args.json).resolve()} (no secrets, safe to paste into a chat)")
    return 0 if rep.ok else 1


def main(argv: list[str] | None = None) -> int:
    from .node_env import ensure_node

    ensure_node()
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command is None:
        parser.print_help()
        return 0
    handlers = {"doctor": cmd_doctor, "plan": cmd_plan, "run": cmd_run, "watch": cmd_watch,
                "roles": cmd_roles, "init-plan": cmd_init_plan, "ledger": cmd_ledger}
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
