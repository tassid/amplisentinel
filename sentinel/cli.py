"""Command line interface.

Every command acts on one workspace (one research project), chosen with
--workspace, the SENTINEL_WORKSPACE variable, or the current folder.

Output rule: every command prints data (values, states, file paths, hashes).
No command prints recommendations.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from . import __version__, deblur, fetch, listen, reconcile
from . import inventory as inv
from . import queries as q
from . import run as run_worker
from .ext import ToolError
from .runs import ConfigError
from .state import TransitionError
from .workspace import Workspace, WorkspaceError


def _ws(args) -> Workspace:
    return Workspace.open(args.workspace)


def cmd_new(args) -> int:
    ws = Workspace.create(args.path, args.name, args.title)
    ws.ledger.append("workspace_created", {"name": ws.name, "version": __version__})
    print(f"workspace criado: {ws.root}")
    return 0


def cmd_init(args) -> int:
    ws = _ws(args)
    ws.ledger.append("init", {"version": __version__, "workspace": ws.name})
    new = ws.state.register(ws.configured_runs())
    print(f"workspace: {ws.name}")
    print(f"corridas registradas: {len(new)}")
    print(f"ledger: {ws.ledger.path}")
    return 0


def cmd_status(args) -> int:
    data = q.status(_ws(args))
    if not data["runs"]:
        print(f"{data['workspace']}: nenhuma corrida registrada")
        return 0
    width = max(len(r) for r in data["runs"])
    print(f"workspace: {data['workspace']}")
    for run, st in data["runs"].items():
        print(f"{run.ljust(width)}  {st}")
    return 0


def cmd_move(args) -> int:
    ws = _ws(args)
    try:
        ws.state.move(args.run, args.to.upper(), args.justification)
    except TransitionError as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    print(f"{ws.name}/{args.run} -> {args.to.upper()}")
    return 0


def cmd_decide(args) -> int:
    ws = _ws(args)
    entry = ws.ledger.append("decision", {"run": args.run, "text": args.text}, actor="human")
    print(f"decisão registrada: {entry['hash'][:12]}")
    return 0


def cmd_inventory(args) -> int:
    ws = _ws(args)
    summary = inv.build(ws.accessions_dir, ws.inventory_dir)
    ws.ledger.append("inventory_built", summary)
    print(f"workspace: {ws.name}")
    print(f"estudos: {summary['projects']}")
    print(f"accessions: {summary['accessions']}")
    print(f"não encontrados no ENA: {summary['not_found']}")
    for path, digest in summary["files"].items():
        print(f"{path}  sha256={digest[:16]}")
    if args.drive:
        rec = reconcile.build(ws)
        print(f"reconciliação (Drive {reconcile.DRIVE_REMOTE}, sem download):")
        print(f"arquivos FASTQ esperados: {rec['files']}")
        print(f"presentes no Drive: {rec['present_in_drive']}")
        print(f"MD5 iguais ao ENA: {rec['equal']}")
        if rec["runs_without_drive_path"]:
            print(f"corridas sem drive_path (não consultadas): {', '.join(rec['runs_without_drive_path'])}")
        print(f"{rec['output']['path']}  sha256={rec['output']['sha256'][:16]}")
    return 0


def cmd_fetch(args) -> int:
    ws = _ws(args)
    try:
        res = fetch.run_fetch(ws, args.run, via=args.via, source=args.source)
    except fetch.FetchRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    print(f"{ws.name}/{res['run']} -> {res['state']}")
    print(f"arquivos: {res['files']}  MD5 igual ao ENA: {res['equal']}")
    for p in res["problems"]:
        where = p.get("file") or p.get("accession") or res["run"]
        log = f"  log: {p['log']['path']}" if p.get("log") else ""
        print(f"  {where}: {p['reason']}{log}")
    print(f"{res['manifest']['path']}  sha256={res['manifest']['sha256'][:16]}")
    return 0 if res["state"] == "MD5_OK" else 1


def cmd_run(args) -> int:
    ws = _ws(args)
    if args.denoiser == "deblur":
        try:
            res = deblur.run_deblur(ws, args.run, trim_length=args.trim_length)
        except run_worker.RunRefused as exc:
            print(f"recusado: {exc}", file=sys.stderr)
            return 2
        print(f"{ws.name}/{res['run']} ({res['experiment_id']}) -> {res['status']}")
        if res.get("error_log"):
            print(f"erro literal: {res['error_log']['path']}  sha256={res['error_log']['sha256'][:16]}")
        print(f"{res['manifest']['path']}  sha256={res['manifest']['sha256'][:16]}")
        return 0 if res["status"] == "COMPLETED" else 1
    try:
        res = run_worker.run_ampliseq(ws, args.run, weblog_port=args.weblog_port)
    except run_worker.RunRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    print(f"{ws.name}/{res['run']} -> {res['state']}")
    print(f"amostras: {res['samples']}")
    print(f"samplesheet: {res['samplesheet']['path']}  sha256={res['samplesheet']['sha256'][:16]}")
    print(f"params: {res['params']['path']}  sha256={res['params']['sha256'][:16]}")
    if res.get("error_log"):
        print(f"erro literal: {res['error_log']['path']}  sha256={res['error_log']['sha256'][:16]}")
    print(f"{res['manifest']['path']}  sha256={res['manifest']['sha256'][:16]}")
    return 0 if res["state"] == "PROCESSING" else 1


def cmd_verify(args) -> int:
    data = q.ledger_verify(_ws(args))
    if data["ok"]:
        print(f"ledger íntegro: {data['entries']} entradas")
        return 0
    print(f"ledger quebrado na linha {data['broken_at_line']}: {data['reason']}")
    return 1


def cmd_show(args) -> int:
    ws = _ws(args)
    fn = {
        "workspace": lambda: q.workspace_info(ws),
        "run": lambda: q.run_detail(ws, args.arg),
        "metrics": lambda: q.run_metrics(ws, args.arg),
        "inventory": lambda: q.inventory(ws, args.arg),
        "ledger": lambda: q.ledger_tail(ws, int(args.arg or 20)),
        "errors": lambda: q.errors(ws, args.arg),
    }[args.what]
    try:
        print(json.dumps(fn(), indent=2, ensure_ascii=False))
    except q.QueryError as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    return 0


def cmd_mcp(args) -> int:
    from .mcp_server import serve

    serve(_ws(args), http=args.http, host=args.host, port=args.port)
    return 0


def cmd_listen(args) -> int:
    return listen.serve(_ws(args), port=args.port, default_run=args.run)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="sentinel", description=__doc__)
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument(
        "-w",
        "--workspace",
        default=os.environ.get("SENTINEL_WORKSPACE", "."),
        help="pasta do projeto (padrão: SENTINEL_WORKSPACE ou a pasta atual)",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("new", help="cria um workspace para um projeto")
    s.add_argument("path")
    s.add_argument("--name", required=True)
    s.add_argument("--title")
    s.set_defaults(func=cmd_new)

    s = sub.add_parser("init", help="registra as corridas de config/runs.yaml")
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
    s.add_argument("--drive", action="store_true", help="também cruza com o Drive (rclone, sem download)")
    s.set_defaults(func=cmd_inventory)

    s = sub.add_parser("fetch", help="baixa os FASTQ de uma corrida e confere o MD5 do ENA")
    s.add_argument("run")
    s.add_argument("--via", choices=["direct", "fetchngs"], default="direct",
                   help="direct: rclone/HTTPS (padrão); fetchngs: nf-core/fetchngs travado")
    s.add_argument("--source", choices=["auto", "drive", "ena"], default="auto",
                   help="auto: Drive quando o arquivo está lá, senão ENA")
    s.set_defaults(func=cmd_fetch)

    s = sub.add_parser("run", help="executa nf-core/ampliseq (ou Deblur) para uma corrida")
    s.add_argument("run")
    s.add_argument("--denoiser", choices=["dada2", "deblur"], default="dada2",
                   help="dada2: nf-core/ampliseq (padrão); deblur: experimento separado no QIIME 2")
    s.add_argument("--trim-length", type=int, default=None,
                   help="comprimento de corte para Deblur (ou deblur.trim_length em rules.yaml)")
    s.add_argument("--weblog-port", type=int, default=None,
                   help="porta local de -with-weblog (padrão: sentinel.yaml ou 8765)")
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("listen", help="escuta eventos -with-weblog do Nextflow em 127.0.0.1")
    s.add_argument("--port", type=int, default=None,
                   help="porta local (padrão: run.weblog_port ou 8765)")
    s.add_argument("--run", default=None, help="nome da corrida quando não estiver nos parâmetros do evento")
    s.set_defaults(func=cmd_listen)

    s = sub.add_parser("verify", help="confere a cadeia de hashes do ledger")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("show", help="mostra dados em JSON (o mesmo que o MCP devolve)")
    s.add_argument("what", choices=["workspace", "run", "metrics", "inventory", "ledger", "errors"])
    s.add_argument("arg", nargs="?")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("mcp", help="servidor MCP só de leitura, travado neste workspace")
    s.add_argument("--http", action="store_true", help="Streamable HTTP em vez de stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8701)
    s.set_defaults(func=cmd_mcp)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (WorkspaceError, ConfigError, ToolError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
