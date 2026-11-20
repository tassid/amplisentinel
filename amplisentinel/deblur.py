"""Optional Stage 3b: Deblur denoiser via QIIME 2 in a pinned container.

`amplisentinel run <RUN> --denoiser deblur` runs cutadapt + Deblur inside a pinned
QIIME 2 container over the verified FASTQ files of `<RUN>` as a separate
experiment with its own ID, directory, parameters, and artifact hashes. It
never touches or overwrites DADA2 outputs (`runs/<RUN>/results/` or
`runs/<RUN>/run_manifest.json`) or the DADA2 run state.
"""

from __future__ import annotations

from .common import (
    file_info,
    log_mlflow_run,
    utc_iso,
    utc_stamp,
    write_json_atomic,
    write_text_atomic,
)
from .ext import Runner, ToolError, call, subprocess_runner
from .rules import load_rules
from .run import RunRefused, _load_samplesheet_rows, settings
from .workspace import Workspace


def _qiime_manifest(rows: list[dict[str, str]], paired: bool) -> str:
    if paired:
        lines = ["sample-id\tforward-absolute-filepath\treverse-absolute-filepath"]
        for r in rows:
            lines.append(f"{r['sample']}\t{r['fastq_1']}\t{r['fastq_2']}")
    else:
        lines = ["sample-id\tabsolute-filepath"]
        for r in rows:
            lines.append(f"{r['sample']}\t{r['fastq_1']}")
    return "\n".join(lines) + "\n"


def _qiime2_image(ws: Workspace) -> str:
    meta = ws.meta
    block = meta.get("run") or {}
    image = block.get("qiime2_image")
    if not isinstance(image, str) or not image.strip():
        raise RunRefused("amplisentinel.yaml: falta run.qiime2_image travada por digest (@sha256:...)")
    if "@sha256:" not in image:
        raise RunRefused(
            f"amplisentinel.yaml run.qiime2_image precisa estar travada por digest (@sha256:...): {image}"
        )
    return image.strip()


def run_deblur(
    ws: Workspace,
    run: str,
    *,
    trim_length: int | None = None,
    runner: Runner = subprocess_runner,
) -> dict:
    """Run QIIME 2 Deblur as an isolated experiment without changing DADA2 outputs."""
    runs = ws.runs()
    if run not in runs:
        raise RunRefused(f"corrida desconhecida neste workspace: {run}")
    rc = runs[run]
    cfg = settings(ws)
    image = _qiime2_image(ws)

    current = ws.state.get(run)
    if current == "BLOCKED":
        raise RunRefused(f"{run} está em BLOCKED; sair de BLOCKED exige `amplisentinel move {run} PROCESSING -j`")
    if current not in ("MD5_OK", "PROCESSING", "MEASURED", "PUBLISHED"):
        raise RunRefused(
            f"{run} está em {current}; --denoiser deblur exige MD5_OK, PROCESSING, MEASURED ou PUBLISHED"
        )

    if not rc.region:
        raise RunRefused(f"corrida {run}: falta 'region' em config/runs.yaml")
    if not rc.primers.forward or not rc.primers.reverse:
        raise RunRefused(f"corrida {run}: faltam 'primers.forward' e 'primers.reverse' em config/runs.yaml")

    rules = load_rules(ws.rules_file)
    tl = trim_length if trim_length is not None else (rules.get("deblur") or {}).get("trim_length")
    if tl is None:
        raise RunRefused(
            f"corrida {run}: falta deblur.trim_length em config/rules.yaml (ou --trim-length)"
        )
    if isinstance(tl, bool) or not isinstance(tl, int) or tl < -1:
        raise RunRefused("deblur.trim_length precisa ser inteiro >= -1")

    rows, paired = _load_samplesheet_rows(ws, run, rc)

    stamp = utc_stamp()
    experiment_id = f"{run}-deblur-{stamp}"
    exp_dir = ws.root / "runs" / run / "experiments" / experiment_id
    exp_dir.mkdir(parents=True, exist_ok=True)

    manifest_path = exp_dir / "qiime_manifest.tsv"
    manifest_sha = write_text_atomic(manifest_path, _qiime_manifest(rows, paired))

    demux_qza = exp_dir / "demux.qza"
    trimmed_qza = exp_dir / "trimmed.qza"
    table_qza = exp_dir / "deblur-table.qza"
    seqs_qza = exp_dir / "deblur-rep-seqs.qza"
    stats_qza = exp_dir / "deblur-stats.qza"

    run_dir = ws.root / "runs" / run
    engine = cfg.container_engine
    base_docker = [engine, "run", "--rm", "-v", f"{run_dir}:{run_dir}", image]
    fmt = "PairedEndFastqManifestPhred33V2" if paired else "SingleEndFastqManifestPhred33V2"
    imp_type = "SampleData[PairedEndSequencesWithQuality]" if paired else "SampleData[SequencesWithQuality]"

    import_cmd = [
        *base_docker,
        "qiime", "tools", "import",
        "--type", imp_type,
        "--input-path", str(manifest_path),
        "--output-path", str(demux_qza),
        "--input-format", fmt,
    ]
    if paired:
        cut_cmd = [
            *base_docker,
            "qiime", "cutadapt", "trim-paired",
            "--i-demultiplexed-sequences", str(demux_qza),
            "--p-front-f", rc.primers.forward,
            "--p-front-r", rc.primers.reverse,
            "--o-trimmed-sequences", str(trimmed_qza),
        ]
    else:
        cut_cmd = [
            *base_docker,
            "qiime", "cutadapt", "trim-single",
            "--i-demultiplexed-sequences", str(demux_qza),
            "--p-front", rc.primers.forward,
            "--o-trimmed-sequences", str(trimmed_qza),
        ]
    deblur_cmd = [
        *base_docker,
        "qiime", "deblur", "denoise-16S",
        "--i-demultiplexed-seqs", str(trimmed_qza),
        "--p-trim-length", str(tl),
        "--o-table", str(table_qza),
        "--o-representative-sequences", str(seqs_qza),
        "--o-stats", str(stats_qza),
    ]

    ctx = {"run": run, "experiment_id": experiment_id, "denoiser": "deblur"}
    commands = [import_cmd, cut_cmd, deblur_cmd]
    exp_manifest_path = exp_dir / "experiment.json"

    try:
        for cmd in commands:
            call(ws, cmd, version_argv=[engine, "--version"], runner=runner, context=ctx)
    except ToolError as exc:
        data = {
            "experiment_id": experiment_id,
            "run": run,
            "denoiser": "deblur",
            "generated_at": utc_iso(),
            "status": "FAILED",
            "qiime2_image": image,
            "parameters": {
                "trim_length": tl,
                "primer_fwd": rc.primers.forward,
                "primer_rev": rc.primers.reverse,
                "paired": paired,
            },
            "qiime_manifest": {"path": str(manifest_path), "sha256": manifest_sha},
            "error_log": exc.log,
        }
        m_sha = write_json_atomic(exp_manifest_path, data)
        ws.ledger.append(
            "experiment_recorded",
            {
                **ctx,
                "status": "FAILED",
                "experiment": {"path": str(exp_manifest_path), "sha256": m_sha},
                "error_log": exc.log,
            },
        )
        return {
            "experiment_id": experiment_id,
            "run": run,
            "denoiser": "deblur",
            "status": "FAILED",
            "state": ws.state.get(run),
            "manifest": {"path": str(exp_manifest_path), "sha256": m_sha},
            "error_log": exc.log,
        }

    artifacts = {
        "demux": file_info(demux_qza),
        "trimmed": file_info(trimmed_qza),
        "table": file_info(table_qza),
        "rep_seqs": file_info(seqs_qza),
        "stats": file_info(stats_qza),
    }
    clean_artifacts = {k: v for k, v in artifacts.items() if v is not None}
    params = {
        "denoiser": "deblur",
        "experiment_id": experiment_id,
        "qiime2_image": image,
        "trim_length": tl,
        "primer_fwd": rc.primers.forward,
        "primer_rev": rc.primers.reverse,
        "paired": paired,
    }
    mlflow_info = log_mlflow_run(
        ws,
        run_name=experiment_id,
        params=params,
        metrics={"trim_length": float(tl), "samples": float(len(rows))},
        tags={"run": run, "denoiser": "deblur", "experiment_id": experiment_id},
        artifacts=[p for p in (table_qza, seqs_qza, stats_qza) if p.exists()],
    )
    data = {
        "experiment_id": experiment_id,
        "run": run,
        "denoiser": "deblur",
        "generated_at": utc_iso(),
        "status": "COMPLETED",
        "qiime2_image": image,
        "parameters": params,
        "commands": commands,
        "qiime_manifest": {"path": str(manifest_path), "sha256": manifest_sha},
        "artifacts": clean_artifacts,
        "mlflow": mlflow_info,
    }
    m_sha = write_json_atomic(exp_manifest_path, data)
    ws.ledger.append(
        "experiment_recorded",
        {
            **ctx,
            "status": "COMPLETED",
            "qiime2_image": image,
            "parameters": params,
            "artifacts": clean_artifacts,
            "experiment": {"path": str(exp_manifest_path), "sha256": m_sha},
            "mlflow": mlflow_info,
        },
    )
    return {
        "experiment_id": experiment_id,
        "run": run,
        "denoiser": "deblur",
        "status": "COMPLETED",
        "state": ws.state.get(run),
        "manifest": {"path": str(exp_manifest_path), "sha256": m_sha},
        "artifacts": clean_artifacts,
        "mlflow": mlflow_info,
        "error_log": None,
    }
