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
    return ap


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
    handlers = {"doctor": cmd_doctor}
    handler = handlers.get(args.command)
    if handler is None:  # pragma: no cover
        return 2
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())
