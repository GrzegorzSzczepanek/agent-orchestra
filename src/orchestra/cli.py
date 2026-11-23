"""Punkt wejścia `orchestra`: doctor, plan, run."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import load_config


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--config", default="agents.toml", help="ścieżka do agents.toml (domyślnie ./agents.toml)")


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="orchestra", description="Orkiestrator agentów kodujących")
    sub = ap.add_subparsers(dest="command", required=True)

    d = sub.add_parser("doctor", help="diagnostyka: cmd, logowanie, modele, headless, ACP, inni agenci")
    _add_common(d)
    d.add_argument("--smoke", action="store_true",
                   help="wykonaj prawdziwy test headless (cmd -p), zużywa niewielką ilość kredytów")
    d.add_argument("--model", default="", help="ID modelu do sprawdzenia (domyślnie z agents.toml lub zmiennej)")
    d.add_argument("--timeout", type=float, default=30, help="limit czasu pojedynczej próby w sekundach")
    d.add_argument("--no-others", action="store_true", help="pomiń agentów innych niż commandcode")
    d.add_argument("--json", default="orchestra-doctor.json", help="gdzie zapisać raport JSON")

    pl = sub.add_parser("plan", help="rola planner układa plan zadań (DAG) w JSON")
    _add_common(pl)
    pl.add_argument("goal", help="cel do zaplanowania")
    pl.add_argument("--repo", default=".", help="repozytorium (domyślnie bieżący katalog)")
    pl.add_argument("--out", default="plan.json", help="gdzie zapisać plan")
    pl.add_argument("--via", choices=["headless", "acp"], default="headless", help="jak rozmawiać z plannerem")
    pl.add_argument("--model", default="", help="model planisty (domyślnie z konfiguracji agenta)")

    r = sub.add_parser("run", help="wykonaj plan: worktree, bramka testów, eskalacja, scalenie do gałęzi integracyjnej")
    _add_common(r)
    r.add_argument("plan", help="plik planu JSON")
    r.add_argument("--repo", default=".")
    r.add_argument("--parallel", type=int, default=2, help="równoległość 1 do 3 (domyślnie 2)")
    r.add_argument("--max-failures", type=int, default=2, help="kolejne porażki taniego workera przed eskalacją")
    r.add_argument("--no-escalate", action="store_true", help="bez eskalacji do roli planner")
    r.add_argument("--rounds", type=int, default=0, help="rundy recenzji po zielonych testach, 0 do 2 (domyślnie 0)")
    r.add_argument("--token-budget", type=int, default=400_000, help="tokeny na zadanie, 0 wyłącza limit")
    r.add_argument("--cost-budget", type=float, default=0.0, help="USD na zadanie, 0 wyłącza limit")
    r.add_argument("--worker", default="", help="nadpisz agenta roli implementer")
    r.add_argument("--model", default="", help="model taniego workera (inaczej zmienna z agents.toml)")
    r.add_argument("--timeout", type=float, default=900, help="limit czasu pojedynczego workera w sekundach")
    r.add_argument("--report", default="", help="plik raportu JSON (domyślnie .orchestra/runs/<id>.json)")
    return ap


def cmd_plan(args: argparse.Namespace) -> int:
    from .agents_io import acp_ask, headless_ask, repo_files
    from .plan import PlanError, make_plan
    from .store import EventStore

    cfg, repo = load_config(args.config), Path(args.repo).resolve()
    store = EventStore(repo / ".orchestra" / "events.db")
    ask = (acp_ask(cfg, "planner", repo, store) if args.via == "acp"
           else headless_ask(cfg, "planner", repo, model=args.model))

    async def go():
        return await make_plan(args.goal, ask, repo_files=await repo_files(repo))

    try:
        plan = asyncio.run(go())
    except (PlanError, RuntimeError) as exc:
        print(f"Błąd planowania: {exc}", file=sys.stderr)
        return 1
    Path(args.out).write_text(plan.model_dump_json(indent=2) + "\n", encoding="utf-8")
    print(f"Plan: {len(plan.tasks)} zadań -> {Path(args.out).resolve()}")
    for t in plan.tasks:
        dep = f" (po: {', '.join(t.depends_on)})" if t.depends_on else ""
        print(f"  {t.id} [{t.difficulty}]{dep}: {t.description[:70]}")
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    import json

    from .agents_io import headless_ask, make_reviewer
    from .plan import Plan
    from .scheduler import RunOptions, run_plan
    from .store import EventStore

    cfg, repo = load_config(args.config), Path(args.repo).resolve()
    plan = Plan.model_validate_json(Path(args.plan).read_text(encoding="utf-8"))
    store = EventStore(repo / ".orchestra" / "events.db")
    opts = RunOptions(parallel=args.parallel, max_failures=args.max_failures, escalate=not args.no_escalate,
                      token_budget=args.token_budget, cost_budget_usd=args.cost_budget,
                      discussion_rounds=args.rounds, worker=args.worker, model=args.model, timeout_s=args.timeout)
    reviewer = make_reviewer(headless_ask(cfg, "reviewer", repo)) if opts.discussion_rounds else None
    report = asyncio.run(run_plan(plan, cfg, repo, store, opts, reviewer=reviewer))
    print(report.render())
    out = Path(args.report) if args.report else repo / ".orchestra" / "runs" / f"{report.run_id}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\nRaport: {out}\nPrzejrzyj i scal: git log --oneline {report.integration_branch}")
    return 0 if report.ok else 1


def cmd_doctor(args: argparse.Namespace) -> int:
    from .doctor import run_doctor

    cfg = load_config(args.config)
    rep = asyncio.run(run_doctor(cfg, smoke=args.smoke, model=args.model, timeout=args.timeout,
                                 others=not args.no_others))
    print(rep.render())
    rep.save(args.json)
    print(f"\nRaport JSON: {Path(args.json).resolve()} (bez sekretów, można go wkleić do rozmowy)")
    return 0 if rep.ok else 1


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {"doctor": cmd_doctor, "plan": cmd_plan, "run": cmd_run}
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
