"""Stage 8: between-project PERMANOVA metrics using scikit-bio.

`sentinel between` runs PERMANOVA on beta diversity by study/project and by
diagnosis, recording R2, pseudo-F, p-value, permutations, and seed in
`.sentinel/metrics/between.json` and in the `between_metrics` ledger event.
Numbers only, no interpretive text.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from .common import sha256_file, utc_iso, write_json_atomic
from .workspace import Workspace


class BetweenRefused(Exception):
    """Between-project calculation could not start."""


def _permanova_r2(pseudo_f: float, sample_size: int, groups: int) -> float:
    df1 = groups - 1
    df2 = sample_size - groups
    if df1 <= 0 or df2 <= 0:
        return 0.0
    num = df1 * pseudo_f
    den = num + df2
    return float(num / den) if den > 0 else 0.0


def _load_distance_matrix(
    *,
    matrix_path: Path | None,
    table_path: Path | None,
    sample_ids: list[str],
    metric: str,
) -> tuple[Any, Path]:
    from skbio.diversity import beta_diversity
    from skbio.stats.distance import DistanceMatrix

    if matrix_path is not None:
        df = pd.read_csv(matrix_path, sep="\t", index_col=0)
        df.index = df.index.astype(str)
        df.columns = df.columns.astype(str)
        common = [s for s in sample_ids if s in df.index and s in df.columns]
        if len(common) < 3:
            raise BetweenRefused("matriz de distância tem menos de 3 amostras em comum com os metadados")
        sub = df.loc[common, common].to_numpy(dtype=float)
        sub = 0.5 * (sub + sub.T)
        np.fill_diagonal(sub, 0.0)
        return DistanceMatrix(sub, ids=common), matrix_path

    if table_path is None:
        raise BetweenRefused("informe --matrix ou --table")
    tdf = pd.read_csv(table_path, sep="\t", index_col=0)
    tdf.index = tdf.index.astype(str)
    tdf.columns = tdf.columns.astype(str)

    # Support either samples-as-columns (ASV_ID x sample) or samples-as-rows.
    col_matches = [s for s in sample_ids if s in tdf.columns]
    row_matches = [s for s in sample_ids if s in tdf.index]
    if len(col_matches) >= len(row_matches):
        common = col_matches
        counts_df = tdf[common].T
    else:
        common = row_matches
        counts_df = tdf.loc[common]

    if len(common) < 3:
        raise BetweenRefused("tabela tem menos de 3 amostras em comum com os metadados")

    counts = counts_df.to_numpy(dtype=float)
    dm = beta_diversity(metric, counts, ids=common)
    return dm, table_path


def run_between(
    ws: Workspace,
    *,
    metadata_path: Path,
    matrix_path: Path | None = None,
    table_path: Path | None = None,
    study_col: str = "study",
    diagnosis_col: str = "diagnosis",
    permutations: int = 999,
    seed: int = 42,
    metric: str = "braycurtis",
) -> dict[str, Any]:
    """Compute PERMANOVA by study and diagnosis with a fixed RNG seed."""
    from skbio.stats.distance import permanova

    if not metadata_path.exists():
        raise BetweenRefused(f"arquivo de metadados não encontrado: {metadata_path}")
    if matrix_path is not None and not matrix_path.exists():
        raise BetweenRefused(f"matriz de distância não encontrada: {matrix_path}")
    if table_path is not None and not table_path.exists():
        raise BetweenRefused(f"tabela de abundância não encontrada: {table_path}")
    if permutations < 1:
        raise BetweenRefused("permutations precisa ser >= 1")

    meta_df = pd.read_csv(metadata_path, sep="\t")
    if meta_df.empty:
        raise BetweenRefused("metadados vazios")
    id_col = next((c for c in ("sample", "ID", "sample_id") if c in meta_df.columns), meta_df.columns[0])
    meta_df[id_col] = meta_df[id_col].astype(str)
    meta_df = meta_df.set_index(id_col)

    for col in (study_col, diagnosis_col):
        if col not in meta_df.columns:
            raise BetweenRefused(f"coluna '{col}' ausente em {metadata_path}")

    dm, input_src_path = _load_distance_matrix(
        matrix_path=matrix_path,
        table_path=table_path,
        sample_ids=list(meta_df.index),
        metric=metric,
    )
    aligned = meta_df.loc[list(dm.ids)]

    results: dict[str, dict[str, Any]] = {}
    for label, col in (("study", study_col), ("diagnosis", diagnosis_col)):
        grouping = aligned[col].astype(str)
        n_groups = int(grouping.nunique())
        if n_groups < 2:
            raise BetweenRefused(f"coluna '{col}' precisa de pelo menos 2 grupos distintos")
        res = permanova(dm, aligned, column=col, permutations=permutations, seed=seed)
        f_val = float(res["test statistic"])
        p_val = float(res["p-value"])
        n_samples = int(res["sample size"])
        perms = int(res["number of permutations"])
        r2 = _permanova_r2(f_val, n_samples, n_groups)
        results[label] = {
            "column": col,
            "groups": n_groups,
            "sample_size": n_samples,
            "permutations": perms,
            "pseudo_F": round(f_val, 6),
            "R2": round(r2, 6),
            "p_value": round(p_val, 6),
        }

    out_path = ws.metrics_dir / "between.json"
    payload = {
        "workspace": ws.name,
        "generated_at": utc_iso(),
        "seed": seed,
        "permutations": permutations,
        "metric": metric if matrix_path is None else "precomputed",
        "sample_size": len(dm.ids),
        "sources": {
            "input": {"path": str(input_src_path), "sha256": sha256_file(input_src_path)},
            "metadata": {"path": str(metadata_path), "sha256": sha256_file(metadata_path)},
        },
        "results": results,
    }
    out_sha = write_json_atomic(out_path, payload)

    ws.ledger.append(
        "between_metrics",
        {
            "run": "between",
            "seed": seed,
            "permutations": permutations,
            "metric": payload["metric"],
            "sources": payload["sources"],
            "results": results,
            "output": {"path": str(out_path), "sha256": out_sha},
        },
    )
    return {
        "output": {"path": str(out_path), "sha256": out_sha},
        "seed": seed,
        "permutations": permutations,
        "results": results,
    }
