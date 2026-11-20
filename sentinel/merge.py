"""Stage 7: project-wide QIIME 2 merge, SILVA taxonomy, and Provenance Replay.

`sentinel merge`:
1. Requires every configured run to be in `PUBLISHED` or `CLEANED`.
2. Checks `region` across `runs.yaml`. If regions differ and no human decision
   has been recorded with `sentinel decide merge "..."`, stops in `BLOCKED`
   and lists the regions by run.
3. Verifies the pinned SILVA classifier MD5 against `sentinel.yaml`, runs
   `qiime feature-table merge`, `qiime feature-table merge-seqs`,
   `qiime feature-classifier classify-sklearn`, and
   `qiime tools replay-supplement` in the pinned QIIME 2 container.
4. Appends `merge` to the ledger with strategy, classifier MD5, artifact hashes,
   and the `replay-supplement` archive hash.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from .common import file_info, md5_file, sha256_file, utc_iso, write_json_atomic
from .ext import Runner, ToolError, call, subprocess_runner
from .run import ENGINES
from .runs import ConfigError
from .workspace import Workspace

MERGE_KEYS = ("qiime2_image", "silva_classifier", "silva_md5")


class MergeRefused(Exception):
    """Merge could not start."""


def merge_settings(ws: Workspace) -> dict[str, str]:
    """Read and validate `merge` settings from sentinel.yaml."""
    meta = ws.meta
    block = meta.get("merge") or {}
    if not isinstance(block, dict):
        raise ConfigError("sentinel.yaml: 'merge' precisa ser um mapa")
    unknown = sorted(str(k) for k in block if k not in MERGE_KEYS)
    if unknown:
        raise ConfigError(
            f"sentinel.yaml merge: chave desconhecida {unknown}; aceitas: {', '.join(MERGE_KEYS)}"
        )
    run_block = meta.get("run") or {}
    image = block.get("qiime2_image") or (run_block.get("qiime2_image") if isinstance(run_block, dict) else None)
    if not isinstance(image, str) or not image.strip() or "@sha256:" not in image:
        raise MergeRefused(
            "sentinel.yaml: falta imagem QIIME 2 travada por digest (@sha256:...) em merge.qiime2_image ou run.qiime2_image"
        )
    silva = block.get("silva_classifier")
    silva_md5 = block.get("silva_md5")
    if not isinstance(silva, str) or not silva.strip():
        raise MergeRefused("sentinel.yaml: falta merge.silva_classifier")
    if not isinstance(silva_md5, str) or not silva_md5.strip():
        raise MergeRefused("sentinel.yaml: falta merge.silva_md5")
    engine = meta.get("container_engine", "docker")
    if engine not in ENGINES:
        raise ConfigError(f"sentinel.yaml container_engine: aceitos {', '.join(ENGINES)}")
    return {
        "qiime2_image": image.strip(),
        "silva_classifier": silva.strip(),
        "silva_md5": silva_md5.strip().lower(),
        "container_engine": engine,
    }


def _find_run_artifacts(ws: Workspace, run: str) -> tuple[Path, Path]:
    q2 = ws.root / "runs" / run / "results" / "qiime2"
    table_candidates = [
        q2 / "abundance_tables" / "filtered-table.qza",
        q2 / "input" / "table.qza",
    ]
    seq_candidates = [
        q2 / "representative_sequences" / "filtered-sequences.qza",
        q2 / "input" / "rep-seqs.qza",
    ]
    table = next((p for p in table_candidates if p.exists()), None)
    seqs = next((p for p in seq_candidates if p.exists()), None)
    if table is None or seqs is None:
        raise MergeRefused(f"corrida {run}: faltam artefatos .qza de tabela/sequências em {q2}")
    return table, seqs


def _latest_merge_decision(ws: Workspace) -> str | None:
    decisions = [
        e for e in ws.ledger.entries()
        if e.get("event") == "decision"
        and e.get("actor") == "human"
        and (e.get("data") or {}).get("run") == "merge"
    ]
    if not decisions:
        return None
    text = (decisions[-1].get("data") or {}).get("text")
    return str(text) if text else None


def run_merge(ws: Workspace, *, runner: Runner = subprocess_runner) -> dict[str, Any]:
    """Merge all PUBLISHED/CLEANED runs after checking regions and SILVA MD5."""
    runs = ws.runs()
    if not runs:
        raise MergeRefused("nenhuma corrida configurada em config/runs.yaml")

    not_ready = {r: ws.state.get(r) for r in sorted(runs) if ws.state.get(r) not in ("PUBLISHED", "CLEANED")}
    if not_ready:
        raise MergeRefused(f"merge exige todas as corridas em PUBLISHED ou CLEANED; pendentes: {not_ready}")

    regions_by_run = {r: cfg.region for r, cfg in runs.items()}
    missing_region = sorted(r for r, reg in regions_by_run.items() if not reg)
    if missing_region:
        raise MergeRefused(f"corridas sem region em config/runs.yaml: {', '.join(missing_region)}")

    merge_dir = ws.home / "merge"
    merge_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = merge_dir / "merge_manifest.json"

    unique_regions = sorted({str(reg) for reg in regions_by_run.values()})
    strategy = _latest_merge_decision(ws)
    if len(unique_regions) > 1 and not strategy:
        manifest = {
            "workspace": ws.name,
            "generated_at": utc_iso(),
            "status": "BLOCKED",
            "reason": "regiões diferentes entre corridas",
            "regions_by_run": regions_by_run,
        }
        m_sha = write_json_atomic(manifest_path, manifest)
        ws.ledger.append(
            "error",
            {
                "run": "merge",
                "stage": "merge",
                "reason": "regiões diferentes entre corridas; registre decisão com `sentinel decide merge`",
                "regions_by_run": regions_by_run,
                "manifest": {"path": str(manifest_path), "sha256": m_sha},
            },
        )
        return {
            "status": "BLOCKED",
            "regions_by_run": regions_by_run,
            "strategy": None,
            "manifest": {"path": str(manifest_path), "sha256": m_sha},
        }

    cfg = merge_settings(ws)
    silva_path = Path(cfg["silva_classifier"])
    if not silva_path.is_absolute():
        silva_path = (ws.root / silva_path).resolve()
    if not silva_path.exists():
        ws.ledger.append(
            "error",
            {"run": "merge", "stage": "merge", "reason": f"classificador SILVA não encontrado: {silva_path}"},
        )
        return {"status": "BLOCKED", "reason": f"classificador SILVA não encontrado: {silva_path}", "regions_by_run": regions_by_run}

    got_md5 = md5_file(silva_path)
    if got_md5 != cfg["silva_md5"]:
        ws.ledger.append(
            "error",
            {
                "run": "merge",
                "stage": "merge",
                "reason": "MD5 do classificador SILVA diferente do configurado",
                "expected_md5": cfg["silva_md5"],
                "obtained_md5": got_md5,
            },
        )
        return {
            "status": "BLOCKED",
            "reason": f"MD5 do SILVA ({got_md5}) difere do esperado ({cfg['silva_md5']})",
            "regions_by_run": regions_by_run,
        }

    tables: list[Path] = []
    seqs: list[Path] = []
    for r in sorted(runs):
        t, s = _find_run_artifacts(ws, r)
        tables.append(t)
        seqs.append(s)

    merged_table = merge_dir / "merged-table.qza"
    merged_seqs = merge_dir / "merged-rep-seqs.qza"
    merged_tax = merge_dir / "merged-taxonomy.qza"
    replay_zip = merge_dir / "replay-supplement.zip"

    engine = cfg["container_engine"]
    image = cfg["qiime2_image"]
    base = [engine, "run", "--rm", "-v", f"{ws.root}:{ws.root}", image]

    cmd_tables = [*base, "qiime", "feature-table", "merge"]
    for t in tables:
        cmd_tables.extend(["--i-tables", str(t)])
    cmd_tables.extend(["--o-merged-table", str(merged_table)])

    cmd_seqs = [*base, "qiime", "feature-table", "merge-seqs"]
    for s in seqs:
        cmd_seqs.extend(["--i-data", str(s)])
    cmd_seqs.extend(["--o-merged-data", str(merged_seqs)])

    cmd_tax = [
        *base,
        "qiime", "feature-classifier", "classify-sklearn",
        "--i-classifier", str(silva_path),
        "--i-reads", str(merged_seqs),
        "--o-classification", str(merged_tax),
    ]
    cmd_replay = [
        *base,
        "qiime", "tools", "replay-supplement",
        "--i-in-fp", str(merged_table),
        "--i-in-fp", str(merged_seqs),
        "--i-in-fp", str(merged_tax),
        "--o-out-fp", str(replay_zip),
    ]

    ctx = {"run": "merge", "stage": "merge"}
    try:
        for cmd in (cmd_tables, cmd_seqs, cmd_tax, cmd_replay):
            call(ws, cmd, version_argv=[engine, "--version"], runner=runner, context=ctx)
    except ToolError as exc:
        return {
            "status": "BLOCKED",
            "reason": str(exc),
            "error_log": exc.log,
            "regions_by_run": regions_by_run,
        }

    artifacts = {
        "merged_table": file_info(merged_table),
        "merged_rep_seqs": file_info(merged_seqs),
        "merged_taxonomy": file_info(merged_tax),
        "replay_supplement": file_info(replay_zip),
    }
    manifest = {
        "workspace": ws.name,
        "generated_at": utc_iso(),
        "status": "COMPLETED",
        "runs": sorted(runs),
        "regions_by_run": regions_by_run,
        "strategy": strategy,
        "qiime2_image": image,
        "silva_classifier": {
            "path": str(silva_path),
            "md5": got_md5,
            "sha256": sha256_file(silva_path),
        },
        "artifacts": {k: v for k, v in artifacts.items() if v is not None},
    }
    m_sha = write_json_atomic(manifest_path, manifest)

    ws.ledger.append(
        "merge",
        {
            "run": "merge",
            "runs": sorted(runs),
            "regions_by_run": regions_by_run,
            "strategy": strategy,
            "qiime2_image": image,
            "silva_classifier": manifest["silva_classifier"],
            "artifacts": manifest["artifacts"],
            "manifest": {"path": str(manifest_path), "sha256": m_sha},
        },
    )
    return {
        "status": "COMPLETED",
        "runs": sorted(runs),
        "regions_by_run": regions_by_run,
        "strategy": strategy,
        "artifacts": manifest["artifacts"],
        "manifest": {"path": str(manifest_path), "sha256": m_sha},
    }
