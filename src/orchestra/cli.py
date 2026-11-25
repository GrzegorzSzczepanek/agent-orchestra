"""Punkt wejścia `orchestra`: doctor, plan, run."""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

from .config import load_config
from .worktree import ensure_local_exclude


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

    w = sub.add_parser("watch", help="podgląd na żywo przebiegów i delegowań (TUI, tylko odczyt)")
    w.add_argument("--repo", default=".", help="repozytorium, którego log zdarzeń oglądasz")
    w.add_argument("--db", default=None, help="ścieżka bazy zdarzeń (domyślnie <repo>/.orchestra/events.db)")

    ro = sub.add_parser("roles", help="kto pełni role planner, reviewer, implementer (z fallbackiem) i zmiana wyboru")
    _add_common(ro)
    ro.add_argument("--repo", default=".")
    ro.add_argument("--set", action="append", default=[], metavar="ROLA=AGENT",
                    help="wybierz agenta roli, np. planner=opencode (pusta wartość kasuje wybór)")
    ro.add_argument("--model", action="append", default=[], metavar="AGENT=MODEL",
                    help="model agenta, np. opencode=opencode/big-pickle (pusta wartość kasuje wybór)")

    lg = sub.add_parser("ledger", help="rejestr delegacji (opłacalność) i budżet sesji workerów")
    lsub = lg.add_subparsers(dest="ledger_cmd", required=True)
    for name, text in (("add", "zapisz recenzję delegacji"), ("show", "tabela delegacji i proponowany próg"),
                       ("budget", "budżet sesji workerów w USD")):
        p = lsub.add_parser(name, help=text)
        _add_common(p)
        p.add_argument("--repo", default=".")
    la = lsub.choices["add"]
    la.add_argument("run_id")
    la.add_argument("task_id")
    la.add_argument("--outcome", required=True, choices=["accepted", "fixed", "rejected"],
                    help="accepted: bez poprawek, fixed: z poprawkami, rejected: odrzucone i zrobione samemu")
    la.add_argument("--spec-file", action="append", default=[], help="plik z opisem lub testem napisanym przez mocny model")
    la.add_argument("--diff-branch", default="", help="gałąź z wynikiem workera (rozmiar diffu to koszt recenzji)")
    la.add_argument("--notes-chars", type=int, default=0, help="liczba znaków uwag z recenzji")
    la.add_argument("--note", default="")
    lb = lsub.choices["budget"]
    lb.add_argument("--set", type=float, default=None, metavar="USD", help="zacznij nową sesję z limitem w USD (0 wyłącza)")
    lb.add_argument("--check", action="store_true", help="kod wyjścia 3, gdy budżet przekroczony")

    ip = sub.add_parser("init-plan", help="zapisz szablon planu JSON do ręcznego podziału zadań (potem: orchestra run)")
    ip.add_argument("out", nargs="?", default="plan.json")

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
    r.add_argument("--integration-test", default="", metavar="KOMENDA",
                   help="pełne testy po każdym scaleniu (przy porażce merge jest cofany)")
    r.add_argument("--no-red-check", action="store_true", help="nie wymagaj, żeby test_cmd failował przed zmianami workera")
    r.add_argument("--report", default="", help="plik raportu JSON (domyślnie .orchestra/runs/<id>.json)")
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
    print(f"\nRaport: {out}\nPrzejrzyj i scal: git log --oneline {report.integration_branch}")
    return 0 if report.ok else 1


PLAN_TEMPLATE = {
    "goal": "Opis całego celu jednym zdaniem",
    "tasks": [
        {"id": "modul-a", "description": "Samowystarczalny opis: pliki, sygnatury, oczekiwane zachowanie.",
         "files": ["src/modul_a.py", "tests/test_modul_a.py"], "acceptance": ["test_cmd przechodzi"],
         "test_cmd": "pytest -q tests/test_modul_a.py", "cwd": "", "difficulty": "easy", "depends_on": []},
        {"id": "modul-b", "description": "Zadanie zależne od modul-a (startuje po jego scaleniu).",
         "files": ["src/modul_b.py", "tests/test_modul_b.py"], "acceptance": ["test_cmd przechodzi"],
         "test_cmd": "pytest -q tests/test_modul_b.py", "cwd": "", "difficulty": "medium", "depends_on": ["modul-a"]},
    ],
}


def cmd_init_plan(args: argparse.Namespace) -> int:
    import json

    from .plan import Plan

    out = Path(args.out)
    if out.exists():
        print(f"{out} już istnieje, nic nie nadpisuję.", file=sys.stderr)
        return 1
    Plan.model_validate(PLAN_TEMPLATE)  # szablon ma być zawsze poprawny
    out.write_text(json.dumps(PLAN_TEMPLATE, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Szablon planu: {out.resolve()}\nEdytuj i uruchom: orchestra run {out} --repo .  (albo przycisk Plan w TUI)")
    return 0


def cmd_roles(args: argparse.Namespace) -> int:
    from .config import save_overrides

    repo = Path(args.repo).resolve()
    cfg = load_config(args.config, repo=repo)
    try:
        roles = dict(item.split("=", 1) for item in args.set)
        models = dict(item.split("=", 1) for item in args.model)
    except ValueError:
        print("Użycie: --set rola=agent, --model agent=model", file=sys.stderr)
        return 2
    bad = [r for r in roles if r not in ("planner", "reviewer", "implementer")] + \
          [a for a in [*roles.values(), *models] if a and a not in cfg.agents]
    if bad:
        print(f"Nieznane: {', '.join(bad)}. Agenci: {', '.join(cfg.agents)}", file=sys.stderr)
        return 2
    if roles or models:
        ensure_local_exclude(repo)
        save_overrides(repo, roles=roles, models=models)
        cfg.reload_overrides(repo)
    for role in ("planner", "reviewer", "implementer"):
        info = cfg.role_info(role)
        note = f"  ({info.reason}; w agents.toml: {info.configured})" if info.changed else ""
        print(f"{role:12} {info.effective}{note}")
    print("\nAgenci:")
    for name, agent in cfg.agents.items():
        model = agent.resolve_model() or "-"
        print(f"  {name:12} {'zainstalowany' if agent.is_available() else 'brak w PATH':14} {agent.tier:7} model: {model}")
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
            print(f"Brak prób zadania {args.task_id} w przebiegu {args.run_id}.", file=sys.stderr)
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
        print(f"Zapisano {args.task_id}: worker {tin}/{tout} tok (~{usd:.4f} USD), opis ~{spec}, recenzja ~{review}, "
              f"diff ~{diff_tokens} (tokeny mocnego modelu to szacunek: znaki / {ledger.CHARS_PER_TOKEN})")
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
            print("Budżet sesji: nieustawiony (orchestra ledger budget --set USD).")
            return 0
        flag = "  PRZEKROCZONY" if state.exceeded else ""
        print(f"Budżet sesji: {state.spent_usd:.4f} z {state.limit_usd:.2f} USD (szacunek z cen w agents.toml){flag}")
        if state.unpriced_agents:
            print(f"Uwaga: brak ceny w agents.toml dla: {', '.join(state.unpriced_agents)} (liczone jako 0).")
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
    print(f"\nRaport JSON: {Path(args.json).resolve()} (bez sekretów, można go wkleić do rozmowy)")
    return 0 if rep.ok else 1


def main(argv: list[str] | None = None) -> int:
    from .node_env import ensure_node

    ensure_node()
    args = build_parser().parse_args(argv)
    handlers = {"doctor": cmd_doctor, "plan": cmd_plan, "run": cmd_run, "watch": cmd_watch,
                "roles": cmd_roles, "init-plan": cmd_init_plan, "ledger": cmd_ledger}
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
