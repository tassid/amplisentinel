"""Command line interface.

Output rule: every command prints data (values, states, file paths, hashes).
No command prints recommendations.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from . import __version__
from . import inventory as inv
from .ledger import Ledger
from .state import RunState, TransitionError

HOME = Path(".sentinel")


def _ledger() -> Ledger:
    return Ledger(HOME / "ledger.jsonl")


def _state() -> RunState:
    return RunState(HOME / "state.json", _ledger())


def cmd_init(args) -> int:
    HOME.mkdir(exist_ok=True)
    led = _ledger()
    led.append("init", {"version": __version__})
    runs_file = Path(args.runs)
    if runs_file.exists():
        cfg = yaml.safe_load(runs_file.read_text(encoding="utf-8")) or {}
        runs = list((cfg.get("runs") or {}).keys())
        new = _state().register(runs)
        print(f"corridas registradas: {len(new)}")
    print(f"ledger: {led.path}")
    return 0


def cmd_status(args) -> int:
    states = _state().all()
    if not states:
        print("nenhuma corrida registrada")
        return 0
    width = max(len(r) for r in states)
    for run, st in sorted(states.items()):
        print(f"{run.ljust(width)}  {st}")
    return 0


def cmd_move(args) -> int:
    try:
        _state().move(args.run, args.to.upper(), args.justification)
    except TransitionError as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    print(f"{args.run} -> {args.to.upper()}")
    return 0


def cmd_decide(args) -> int:
    entry = _ledger().append(
        "decision", {"run": args.run, "text": args.text}, actor="human"
    )
    print(f"decisão registrada: {entry['hash'][:12]}")
    return 0


def cmd_inventory(args) -> int:
    summary = inv.build(Path(args.accessions), Path(args.out))
    _ledger().append("inventory_built", summary)
    print(f"projetos: {summary['projects']}")
    print(f"accessions: {summary['accessions']}")
    print(f"não encontrados no ENA: {summary['not_found']}")
    for path, digest in summary["files"].items():
        print(f"{path}  sha256={digest[:16]}")
    return 0


def cmd_verify(args) -> int:
    res = _ledger().verify()
    if res.ok:
        print(f"ledger íntegro: {res.entries} entradas")
        return 0
    print(f"ledger quebrado na linha {res.broken_at}: {res.reason}")
    return 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sentinel", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("init", help="cria .sentinel/ e registra as corridas")
    s.add_argument("--runs", default="config/runs.yaml")
    s.set_defaults(func=cmd_init)

    s = sub.add_parser("status", help="estado de cada corrida")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("move", help="muda o estado de uma corrida")
    s.add_argument("run")
    s.add_argument("to")
    s.add_argument("-j", "--justification", help="obrigatória para sair de BLOCKED")
    s.set_defaults(func=cmd_move)

    s = sub.add_parser("decide", help="registra uma decisão escrita pela pesquisadora")
    s.add_argument("run")
    s.add_argument("text")
    s.set_defaults(func=cmd_decide)

    s = sub.add_parser("inventory", help="accessions x ENA")
    s.add_argument("--accessions", default="data/accessions")
    s.add_argument("--out", default=".sentinel/inventory")
    s.set_defaults(func=cmd_inventory)

    s = sub.add_parser("verify", help="confere a cadeia de hashes do ledger")
    s.set_defaults(func=cmd_verify)

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
