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
from pathlib import Path

from . import (
    __version__,
    between,
    deblur,
    decontam,
    fetch,
    listen,
    measure,
    merge,
    ml,
    publish,
    reconcile,
    report,
    serve_all,
)
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
        "reconciliation": lambda: q.reconciliation(ws, args.arg),
        "fetch-manifest": lambda: q.fetch_manifest(ws, args.arg),
        "process-events": lambda: q.process_events(ws, args.arg),
        "between": lambda: q.between_metrics(ws),
        "folds": lambda: q.folds(ws),
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


def cmd_serve_all(args) -> int:
    serve_all.serve_all(Path(args.config) if args.config else None)
    return 0


def cmd_launchd(args) -> int:
    ws = _ws(args)
    plist = serve_all.render_launchd_plist(ws, port=args.port, sentinel_bin=args.sentinel_bin)
    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(plist, encoding="utf-8")
        print(f"plist salvo: {out}")
    else:
        print(plist, end="")
    return 0


def cmd_report(args) -> int:
    ws = _ws(args)
    res = report.generate_report(ws, out_path=Path(args.out) if args.out else None)
    print(f"relatório: {res['output']['path']}  sha256={res['output']['sha256'][:16]}")
    print(f"corridas={res['runs']}  decisões={res['decisions']}  qzv={res['qzv_count']}")
    return 0


def cmd_listen(args) -> int:
    return listen.serve(_ws(args), port=args.port, default_run=args.run)


def cmd_measure(args) -> int:
    ws = _ws(args)
    try:
        res = measure.measure_run(ws, args.run)
    except measure.MeasureRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    print(f"{ws.name}/{res['run']} -> {res['state']}")
    print(f"métricas: {res['metrics_count']}  violações: {len(res['violations'])}")
    for v in res["violations"]:
        print(f"  {v['metric']}: {v['value']} ({v['result']} {v['rule']})")
    print(f"{res['metrics']['path']}  sha256={res['metrics']['sha256'][:16]}")
    return 0 if res["state"] == "MEASURED" else 1


def cmd_decontam(args) -> int:
    ws = _ws(args)
    try:
        res = decontam.run_decontam(ws, args.run)
    except decontam.DecontamRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    if res["status"] == "skipped":
        print(f"{ws.name}/{res['run']}: {res['reason']}")
        return 0
    print(f"{ws.name}/{res['run']} -> {res['state']} (decontam: {res['status']})")
    if res["status"] == "COMPLETED":
        print(f"ASVs marcados: {len(res['flagged_asvs'])}")
        print(f"{res['manifest']['path']}  sha256={res['manifest']['sha256'][:16]}")
        return 0
    print(f"motivo: {res.get('reason')}")
    return 1


def cmd_publish(args) -> int:
    ws = _ws(args)
    try:
        res = publish.publish_run(ws, args.run)
    except publish.PublishRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    print(f"{ws.name}/{res['run']} -> {res['state']}")
    if res["state"] == "PUBLISHED":
        print(f"arquivos publicados: {res['files']}  destino: {res['remote_root']}")
        print(f"{res['manifest']['path']}  sha256={res['manifest']['sha256'][:16]}")
        return 0
    if res.get("error_log"):
        print(f"erro literal: {res['error_log']['path']}  sha256={res['error_log']['sha256'][:16]}")
    return 1


def cmd_clean(args) -> int:
    ws = _ws(args)
    try:
        res = publish.clean_run(ws, args.run)
    except publish.CleanRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    print(f"{ws.name}/{res['run']} -> {res['state']}")
    print(f"arquivos apagados: {res['deleted_count']}")
    print(f"{res['manifest']['path']}  sha256={res['manifest']['sha256'][:16]}")
    return 0


def cmd_merge(args) -> int:
    ws = _ws(args)
    try:
        res = merge.run_merge(ws)
    except merge.MergeRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    print(f"{ws.name}/merge -> {res['status']}")
    for r, reg in sorted((res.get("regions_by_run") or {}).items()):
        print(f"  {r}: {reg}")
    if res.get("strategy"):
        print(f"estratégia: {res['strategy']}")
    if res.get("manifest"):
        print(f"{res['manifest']['path']}  sha256={res['manifest']['sha256'][:16]}")
    return 0 if res["status"] == "COMPLETED" else 1


def cmd_between(args) -> int:
    ws = _ws(args)
    try:
        res = between.run_between(
            ws,
            metadata_path=Path(args.metadata),
            matrix_path=Path(args.matrix) if args.matrix else None,
            table_path=Path(args.table) if args.table else None,
            study_col=args.study_col,
            diagnosis_col=args.diagnosis_col,
            permutations=args.permutations,
            seed=args.seed,
            metric=args.metric,
        )
    except between.BetweenRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    for label, stats in res["results"].items():
        print(
            f"{label}: R2={stats['R2']}  pseudo_F={stats['pseudo_F']}  "
            f"p={stats['p_value']}  permutations={stats['permutations']}  seed={res['seed']}"
        )
    print(f"{res['output']['path']}  sha256={res['output']['sha256'][:16]}")
    return 0


def cmd_export(args) -> int:
    ws = _ws(args)
    try:
        res = ml.export_features(ws, qza_path=Path(args.qza), table_path=Path(args.table))
    except ml.MLRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    print(f"uuid={res['source_qza']['uuid']}")
    print(f"{res['tsv']['path']}  sha256={res['tsv']['sha256'][:16]}")
    print(f"{res['parquet']['path']}  sha256={res['parquet']['sha256'][:16]}")
    return 0


def cmd_folds(args) -> int:
    ws = _ws(args)
    try:
        res = ml.build_folds(
            ws,
            metadata_path=Path(args.metadata),
            study_col=args.study_col,
            target_col=args.target_col,
        )
    except ml.MLRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    for f in res["folds"]:
        print(
            f"fold {f['fold']} ({f['test_project']}): "
            f"train={f['train_class_counts']}  test={f['test_class_counts']}"
        )
    print(f"{res['folds_file']['path']}  sha256={res['folds_file']['sha256'][:16]}")
    return 0


def cmd_train(args) -> int:
    ws = _ws(args)
    try:
        res = ml.train_models(
            ws,
            metadata_path=Path(args.metadata),
            config_path=Path(args.config) if args.config else None,
        )
    except ml.MLRefused as exc:
        print(f"recusado: {exc}", file=sys.stderr)
        return 2
    print(
        f"seed={res['seed']}  qza_uuid={res['source_qza_uuid']}  "
        f"lopo_acc={res['metrics']['lopo_accuracy_mean']:.4f}"
    )
    print(f"shap: {res['artifacts']['shap_values']['path']}  sha256={res['artifacts']['shap_values']['sha256'][:16]}")
    return 0


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

    s = sub.add_parser("measure", help="extrai métricas do ampliseq e compara com config/rules.yaml")
    s.add_argument("run")
    s.set_defaults(func=cmd_measure)

    s = sub.add_parser("decontam", help="identifica contaminantes quando runs.yaml declara negative_controls")
    s.add_argument("run")
    s.set_defaults(func=cmd_decontam)

    s = sub.add_parser("publish", help="envia artefatos para gdrive_rw, DVC e MLflow e confere hashes")
    s.add_argument("run")
    s.set_defaults(func=cmd_publish)

    s = sub.add_parser("clean", help="apaga FASTQ e work/ locais apenas em PUBLISHED após conferir hashes remotos")
    s.add_argument("run")
    s.set_defaults(func=cmd_clean)

    s = sub.add_parser("merge", help="une corridas PUBLISHED/CLEANED, atribui taxonomia SILVA e gera replay-supplement")
    s.set_defaults(func=cmd_merge)

    s = sub.add_parser("between", help="PERMANOVA entre estudos e diagnósticos (scikit-bio)")
    s.add_argument("--metadata", required=True)
    s.add_argument("--matrix")
    s.add_argument("--table")
    s.add_argument("--study-col", default="study")
    s.add_argument("--diagnosis-col", default="diagnosis")
    s.add_argument("--permutations", type=int, default=999)
    s.add_argument("--seed", type=int, default=42)
    s.add_argument("--metric", default="braycurtis")
    s.set_defaults(func=cmd_between)

    s = sub.add_parser("export", help="exporta tabela para TSV/Parquet gravando UUID do .qza")
    s.add_argument("--qza", required=True)
    s.add_argument("--table", required=True)
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("folds", help="gera folds leave-one-project-out determinísticos")
    s.add_argument("--metadata", required=True)
    s.add_argument("--study-col", default="study")
    s.add_argument("--target-col", default="diagnosis")
    s.set_defaults(func=cmd_folds)

    s = sub.add_parser("train", help="registra Random Forest, t-SNE e SHAP no MLflow via config/ml.yaml")
    s.add_argument("--metadata", required=True)
    s.add_argument("--config")
    s.set_defaults(func=cmd_train)

    s = sub.add_parser("verify", help="confere a cadeia de hashes do ledger")
    s.set_defaults(func=cmd_verify)

    s = sub.add_parser("show", help="mostra dados em JSON (o mesmo que o MCP devolve)")
    s.add_argument(
        "what",
        choices=[
            "workspace",
            "run",
            "metrics",
            "inventory",
            "ledger",
            "errors",
            "reconciliation",
            "fetch-manifest",
            "process-events",
            "between",
            "folds",
        ],
    )
    s.add_argument("arg", nargs="?")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("mcp", help="servidor MCP só de leitura, travado neste workspace")
    s.add_argument("--http", action="store_true", help="Streamable HTTP em vez de stdio")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8701)
    s.set_defaults(func=cmd_mcp)

    s = sub.add_parser("serve-all", help="sobe um servidor MCP por workspace listado em ~/.sentinel/workspaces.yaml")
    s.add_argument("--config", help="caminho alternativo para workspaces.yaml")
    s.set_defaults(func=cmd_serve_all)

    s = sub.add_parser("launchd", help="gera plist do launchd (macOS) para o servidor MCP do workspace")
    s.add_argument("--port", type=int, default=8701)
    s.add_argument("--sentinel-bin", default="sentinel")
    s.add_argument("--out")
    s.set_defaults(func=cmd_launchd)

    s = sub.add_parser("report", help="gera relatório HTML estático determinístico do workspace")
    s.add_argument("--out")
    s.set_defaults(func=cmd_report)

    args = p.parse_args(argv)
    try:
        return args.func(args)
    except (WorkspaceError, ConfigError, ToolError) as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
