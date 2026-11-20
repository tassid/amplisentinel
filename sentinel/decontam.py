"""Optional Stage 5b: contaminant identification with R decontam.

Runs only when `runs.yaml` declares `negative_controls` for the run. Without
that key, nothing happens. With `negative_controls`, requires `decontam.method`
and `decontam.threshold` in `config/rules.yaml`; if missing, moves the run to
`BLOCKED` and reports what is missing without inventing parameters.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pandas as pd

from .common import sha256_file, utc_iso, write_json_atomic, write_text_atomic
from .ext import Runner, ToolError, call, subprocess_runner
from .rules import load_rules
from .run import settings
from .workspace import Workspace


class DecontamRefused(Exception):
    """Decontam did not start. The run state did not change."""


def _decontam_image(ws: Workspace) -> str:
    meta = ws.meta
    block = meta.get("run") or {}
    image = block.get("decontam_image")
    if not isinstance(image, str) or not image.strip():
        raise DecontamRefused("sentinel.yaml: falta run.decontam_image travada por digest (@sha256:...)")
    if "@sha256:" not in image:
        raise DecontamRefused(
            f"sentinel.yaml run.decontam_image precisa estar travada por digest (@sha256:...): {image}"
        )
    return image.strip()


def _r_script(
    asv_table: Path,
    controls_tsv: Path,
    out_table: Path,
    out_details: Path,
    method: str,
    threshold: float,
) -> str:
    return (
        "suppressPackageStartupMessages(library(decontam))\n"
        f'meta <- read.csv("{controls_tsv.as_posix()}", header=TRUE, sep="\\t", stringsAsFactors=FALSE)\n'
        f'ab_tsv <- read.csv("{asv_table.as_posix()}", header=TRUE, sep="\\t", check.names=FALSE)\n'
        "ab <- t(as.matrix(ab_tsv[,-1, drop=FALSE]))\n"
        "colnames(ab) <- ab_tsv[,1]\n"
        "meta <- meta[match(rownames(ab), meta$sample), , drop=FALSE]\n"
        "neg <- meta$control == 'control'\n"
        f'res <- isContaminant(ab, neg=neg, method="{method}", threshold={threshold}, detailed=TRUE)\n'
        "res <- cbind(ASV_ID=rownames(res), res)\n"
        f'write.table(res, file="{out_details.as_posix()}", sep="\\t", row.names=FALSE, quote=FALSE)\n'
        "keep <- res$ASV_ID[!res$contaminant]\n"
        "filtered <- ab_tsv[ab_tsv[,1] %in% keep, , drop=FALSE]\n"
        f'write.table(filtered, file="{out_table.as_posix()}", sep="\\t", row.names=FALSE, quote=FALSE)\n'
    )


def run_decontam(
    ws: Workspace,
    run: str,
    *,
    runner: Runner = subprocess_runner,
) -> dict[str, Any]:
    """Run decontam when `negative_controls` is declared for `run`."""
    runs = ws.runs()
    if run not in runs:
        raise DecontamRefused(f"corrida desconhecida neste workspace: {run}")
    rc = runs[run]
    if not rc.negative_controls:
        return {
            "run": run,
            "status": "skipped",
            "state": ws.state.get(run),
            "reason": "corrida sem negative_controls em config/runs.yaml",
        }

    current = ws.state.get(run)
    if current == "BLOCKED":
        raise DecontamRefused(
            f"{run} está em BLOCKED; sair de BLOCKED exige `sentinel move {run} PROCESSING -j`"
        )
    if current not in ("PROCESSING", "MEASURED"):
        raise DecontamRefused(f"{run} está em {current}; decontam exige PROCESSING ou MEASURED")

    rules = load_rules(ws.rules_file)
    dec_cfg = rules.get("decontam") or {}
    missing = [k for k in ("method", "threshold") if k not in dec_cfg]
    if missing:
        msg = f"faltam {', '.join('decontam.' + k for k in missing)} em config/rules.yaml"
        ws.ledger.append(
            "error",
            {"run": run, "stage": "decontam", "reason": msg, "missing": missing},
        )
        ws.state.move(run, "BLOCKED")
        return {
            "run": run,
            "status": "BLOCKED",
            "state": "BLOCKED",
            "reason": msg,
            "flagged_asvs": [],
        }

    cfg = settings(ws)
    image = _decontam_image(ws)

    asv_table = ws.root / "runs" / run / "results" / "dada2" / "ASV_table.tsv"
    if not asv_table.exists():
        raise DecontamRefused(f"corrida {run}: tabela de ASVs não encontrada: {asv_table}")

    before_sha = sha256_file(asv_table)
    ab_df = pd.read_csv(asv_table, sep="\t")
    sample_cols = [str(c) for c in ab_df.columns[1:]]
    neg_set = set(rc.negative_controls)

    dec_dir = ws.root / "runs" / run / "decontam"
    dec_dir.mkdir(parents=True, exist_ok=True)
    controls_path = dec_dir / "controls.tsv"
    controls_lines = ["sample\tcontrol"]
    for sid in sample_cols:
        controls_lines.append(f"{sid}\t{'control' if sid in neg_set else 'sample'}")
    write_text_atomic(controls_path, "\n".join(controls_lines) + "\n")

    out_table = dec_dir / "decontaminated.tsv"
    out_details = dec_dir / "decontaminated_details.tsv"
    script_path = dec_dir / "run_decontam.R"
    method = str(dec_cfg["method"])
    threshold = float(dec_cfg["threshold"])
    write_text_atomic(
        script_path,
        _r_script(asv_table, controls_path, out_table, out_details, method, threshold),
    )

    run_dir = ws.root / "runs" / run
    engine = cfg.container_engine
    argv = [
        engine,
        "run",
        "--rm",
        "-v",
        f"{run_dir}:{run_dir}",
        image,
        "Rscript",
        str(script_path),
    ]
    ctx = {"run": run, "stage": "decontam"}
    try:
        call(ws, argv, version_argv=[engine, "--version"], runner=runner, context=ctx)
    except ToolError as exc:
        ws.state.move(run, "BLOCKED")
        return {
            "run": run,
            "status": "BLOCKED",
            "state": "BLOCKED",
            "reason": str(exc),
            "error_log": exc.log,
            "flagged_asvs": [],
        }

    if not out_table.exists() or not out_details.exists():
        ws.ledger.append(
            "error",
            {"run": run, "stage": "decontam", "reason": "saídas do decontam não geradas"},
        )
        ws.state.move(run, "BLOCKED")
        return {
            "run": run,
            "status": "BLOCKED",
            "state": "BLOCKED",
            "reason": "saídas do decontam não geradas",
            "flagged_asvs": [],
        }

    after_sha = sha256_file(out_table)
    det_df = pd.read_csv(out_details, sep="\t")
    id_col = det_df.columns[0]
    flagged: list[dict[str, Any]] = []
    for _, row in det_df.iterrows():
        contam_raw = str(row.get("contaminant", "")).strip().lower()
        if contam_raw in {"true", "t", "1"}:
            p_val = row.get("p")
            score = None if pd.isna(p_val) else float(p_val)
            flagged.append({"asv_id": str(row[id_col]), "score": score})

    manifest_path = dec_dir / "decontam_manifest.json"
    manifest = {
        "run": run,
        "generated_at": utc_iso(),
        "image": image,
        "method": method,
        "threshold": threshold,
        "negative_controls": list(rc.negative_controls),
        "table_before": {"path": str(asv_table), "sha256": before_sha},
        "table_after": {"path": str(out_table), "sha256": after_sha},
        "details": {"path": str(out_details), "sha256": sha256_file(out_details)},
        "flagged_asvs": flagged,
    }
    m_sha = write_json_atomic(manifest_path, manifest)

    ws.ledger.append(
        "decontam_recorded",
        {
            "run": run,
            "image": image,
            "method": method,
            "threshold": threshold,
            "negative_controls": list(rc.negative_controls),
            "table_before": manifest["table_before"],
            "table_after": manifest["table_after"],
            "flagged_asvs": flagged,
            "manifest": {"path": str(manifest_path), "sha256": m_sha},
        },
    )
    return {
        "run": run,
        "status": "COMPLETED",
        "state": ws.state.get(run),
        "table_before": manifest["table_before"],
        "table_after": manifest["table_after"],
        "flagged_asvs": flagged,
        "manifest": {"path": str(manifest_path), "sha256": m_sha},
    }
