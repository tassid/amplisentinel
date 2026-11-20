"""Stage 5: measure worker reading nf-core/ampliseq 3.1.0 outputs with pandas.

`amplisentinel measure <RUN>` extracts neutral numeric metrics from DADA2, cutadapt,
ASV lengths, taxonomy, per-sample depth, and MultiQC `multiqc_data/`, compares
each value strictly against thresholds written by the researcher in
`config/rules.yaml`, writes `.amplisentinel/metrics/<RUN>.json`, and transitions the
run to `MEASURED` (or `BLOCKED` if any rule evaluates to `below` or `above`).
Empty `rules.yaml` produces `no_rule` only and never blocks.
"""

from __future__ import annotations

import math
from pathlib import Path
from typing import Any

import pandas as pd

from .common import file_info, log_mlflow_run, sha256_file, utc_iso, write_json_atomic
from .rules import load_rules
from .workspace import Workspace


class MeasureRefused(Exception):
    """Measurement could not run. The run state did not change."""


def _num_series(series: pd.Series) -> pd.Series:
    cleaned = series.astype(str).str.replace(",", "", regex=False).str.strip()
    return pd.to_numeric(cleaned, errors="coerce")


def _round(value: float) -> float:
    return round(float(value), 4)


def _fasta_lengths(path: Path) -> list[int]:
    lengths: list[int] = []
    cur = 0
    in_seq = False
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith(">"):
            if in_seq:
                lengths.append(cur)
            cur = 0
            in_seq = True
        else:
            cur += len(line)
    if in_seq:
        lengths.append(cur)
    return lengths


def _is_unassigned(val: Any) -> bool:
    if val is None:
        return True
    if isinstance(val, float) and math.isnan(val):
        return True
    text = str(val).strip()
    if not text or text.lower() in {"na", "nan", "none", "unassigned", "unclassified"}:
        return True
    return re_suffix_empty(text)


def re_suffix_empty(text: str) -> bool:
    return len(text) >= 3 and text[1:3] == "__" and len(text) == 3


def _eval_rule(
    metric_key: str, value: float, rules: dict[str, dict[str, Any]]
) -> tuple[str | None, str]:
    group, name = metric_key.split(".", 1)
    grp = rules.get(group) or {}
    if name in grp and name.endswith(("_min", "_max")):
        limit = grp[name]
        if name.endswith("_min"):
            return f"{group}.{name}", ("below" if value < limit else "within")
        return f"{group}.{name}", ("above" if value > limit else "within")

    min_key = f"{name}_min"
    max_key = f"{name}_max"
    has_min = min_key in grp
    has_max = max_key in grp
    if has_min and value < grp[min_key]:
        return f"{group}.{min_key}", "below"
    if has_max and value > grp[max_key]:
        return f"{group}.{max_key}", "above"
    if has_min and has_max:
        return f"{group}.{min_key}, {group}.{max_key}", "within"
    if has_min:
        return f"{group}.{min_key}", "within"
    if has_max:
        return f"{group}.{max_key}", "within"
    return None, "no_rule"


def _add_metric(
    values: dict[str, dict[str, Any]],
    rules: dict[str, dict[str, Any]],
    key: str,
    value: float,
    unit: str,
    source: dict[str, Any],
) -> None:
    rounded = _round(value)
    rule_ref, result = _eval_rule(key, rounded, rules)
    values[key] = {
        "value": rounded,
        "unit": unit,
        "rule": rule_ref,
        "result": result,
        "source": source,
    }


def _find_taxonomy_file(outdir: Path) -> Path | None:
    dada2_dir = outdir / "dada2"
    if dada2_dir.exists():
        consolidated = sorted(dada2_dir.glob("ASV_tax.consolidated*.tsv"))
        if consolidated:
            return consolidated[0]
        plain = sorted(
            p for p in dada2_dir.glob("ASV_tax*.tsv")
            if "_species" not in p.name
        )
        if plain:
            return plain[0]
        any_tax = sorted(dada2_dir.glob("ASV_tax*.tsv"))
        if any_tax:
            return any_tax[0]
    q_tax = outdir / "qiime2" / "taxonomy" / "taxonomy.tsv"
    if q_tax.exists():
        return q_tax
    return None


def _parse_multiqc(mqc_dir: Path) -> tuple[dict[str, tuple[float, str, dict]], dict[str, dict]]:
    out: dict[str, tuple[float, str, dict]] = {}
    if not mqc_dir.exists():
        return out, {}

    candidates = [
        mqc_dir / "multiqc_general_stats.txt",
        mqc_dir / "multiqc_fastqc.txt",
    ]
    for path in candidates:
        if not path.exists():
            continue
        df = pd.read_csv(path, sep="\t")
        if df.empty:
            continue
        src = {"path": str(path), "sha256": sha256_file(path)}
        col_map = {str(c).lower(): str(c) for c in df.columns}
        for low, orig in col_map.items():
            if "total_sequences" in low or low == "total sequences":
                vals = _num_series(df[orig]).dropna()
                if not vals.empty and "quality.fastqc_total_sequences_mean" not in out:
                    out["quality.fastqc_total_sequences_mean"] = (float(vals.mean()), "reads", src)
            elif "percent_gc" in low or "% gc" in low:
                vals = _num_series(df[orig]).dropna()
                if not vals.empty and "quality.fastqc_percent_gc" not in out:
                    out["quality.fastqc_percent_gc"] = (float(vals.mean()), "%", src)
            elif "avg_sequence_length" in low or "avg sequence length" in low:
                vals = _num_series(df[orig]).dropna()
                if not vals.empty and "quality.fastqc_avg_sequence_length" not in out:
                    out["quality.fastqc_avg_sequence_length"] = (float(vals.mean()), "bp", src)
        if out:
            break
    return out, {}


def measure_run(ws: Workspace, run: str) -> dict[str, Any]:
    """Read ampliseq outputs for `run`, compare against rules.yaml, and record metrics."""
    runs = ws.runs()
    if run not in runs:
        raise MeasureRefused(f"corrida desconhecida neste workspace: {run}")

    current = ws.state.get(run)
    if current == "BLOCKED":
        raise MeasureRefused(
            f"{run} está em BLOCKED; sair de BLOCKED exige `amplisentinel move {run} PROCESSING -j`"
        )
    if current != "PROCESSING":
        raise MeasureRefused(f"{run} está em {current}; measure só roda em PROCESSING")

    rules = load_rules(ws.rules_file)
    outdir = ws.root / "runs" / run / "results"
    if not outdir.exists():
        raise MeasureRefused(f"corrida {run}: pasta de resultados não encontrada: {outdir}")

    dada2_stats_path = outdir / "dada2" / "DADA2_stats.tsv"
    if not dada2_stats_path.exists():
        raise MeasureRefused(f"corrida {run}: arquivo obrigatório não encontrado: {dada2_stats_path}")

    values: dict[str, dict[str, Any]] = {}
    samples: dict[str, dict[str, Any]] = {}

    d_df = pd.read_csv(dada2_stats_path, sep="\t")
    if d_df.empty or "sample" not in d_df.columns or "DADA2_input" not in d_df.columns:
        raise MeasureRefused(f"corrida {run}: formato inválido em {dada2_stats_path}")
    d_src = {"path": str(dada2_stats_path), "sha256": sha256_file(dada2_stats_path)}

    for col in ("DADA2_input", "filtered", "denoisedF", "denoisedR", "denoisedPairs", "denoised", "merged", "nonchim"):
        if col in d_df.columns:
            d_df[col] = _num_series(d_df[col]).fillna(0)

    for _, row in d_df.iterrows():
        sid = str(row["sample"])
        s_entry = samples.setdefault(sid, {})
        for col in ("DADA2_input", "filtered", "denoisedF", "denoisedR", "denoisedPairs", "denoised", "merged", "nonchim"):
            if col in d_df.columns:
                s_entry[col] = int(row[col])

    total_in = float(d_df["DADA2_input"].sum())
    total_filt = float(d_df["filtered"].sum()) if "filtered" in d_df.columns else 0.0
    total_nonchim = float(d_df["nonchim"].sum()) if "nonchim" in d_df.columns else 0.0
    if total_in > 0:
        _add_metric(values, rules, "dada2.filtered_pct", (total_filt / total_in) * 100.0, "%", d_src)
        _add_metric(values, rules, "dada2.nonchim_pct", (total_nonchim / total_in) * 100.0, "%", d_src)

    if "merged" in d_df.columns:
        total_merged = float(d_df["merged"].sum())
        if total_in > 0:
            _add_metric(values, rules, "dada2.merged_pct", (total_merged / total_in) * 100.0, "%", d_src)
        if total_merged > 0:
            chim_pct = max(0.0, ((total_merged - total_nonchim) / total_merged) * 100.0)
            _add_metric(values, rules, "dada2.chimera_pct", chim_pct, "%", d_src)
    elif "denoised" in d_df.columns:
        total_den = float(d_df["denoised"].sum())
        if total_den > 0:
            chim_pct = max(0.0, ((total_den - total_nonchim) / total_den) * 100.0)
            _add_metric(values, rules, "dada2.chimera_pct", chim_pct, "%", d_src)

    cut_path = outdir / "cutadapt" / "cutadapt_summary.tsv"
    if cut_path.exists():
        c_df = pd.read_csv(cut_path, sep="\t")
        if not c_df.empty and {"cutadapt_total_processed", "cutadapt_passing_filters"}.issubset(c_df.columns):
            c_src = {"path": str(cut_path), "sha256": sha256_file(cut_path)}
            tot = float(_num_series(c_df["cutadapt_total_processed"]).fillna(0).sum())
            pas = float(_num_series(c_df["cutadapt_passing_filters"]).fillna(0).sum())
            if tot > 0:
                _add_metric(values, rules, "primers.found_pct", (pas / tot) * 100.0, "%", c_src)
            if "sample" in c_df.columns:
                for _, row in c_df.iterrows():
                    sid = str(row["sample"])
                    t_v = _num_series(pd.Series([row["cutadapt_total_processed"]])).iloc[0]
                    p_v = _num_series(pd.Series([row["cutadapt_passing_filters"]])).iloc[0]
                    if pd.notna(t_v) and pd.notna(p_v):
                        samples.setdefault(sid, {})["cutadapt_total_processed"] = int(t_v)
                        samples.setdefault(sid, {})["cutadapt_passing_filters"] = int(p_v)

    asv_table_path = outdir / "dada2" / "ASV_table.tsv"
    depths: list[float] = []
    if asv_table_path.exists():
        a_df = pd.read_csv(asv_table_path, sep="\t")
        depth_src = {"path": str(asv_table_path), "sha256": sha256_file(asv_table_path)}
        sample_cols = [c for c in a_df.columns[1:]]
        for col in sample_cols:
            s_depth = float(_num_series(a_df[col]).fillna(0).sum())
            depths.append(s_depth)
            samples.setdefault(str(col), {})["depth"] = int(s_depth)
    elif "nonchim" in d_df.columns:
        depth_src = d_src
        for _, row in d_df.iterrows():
            s_depth = float(row["nonchim"])
            depths.append(s_depth)
            samples.setdefault(str(row["sample"]), {})["depth"] = int(s_depth)

    if depths:
        depth_s = pd.Series(depths)
        _add_metric(values, rules, "depth.reads_per_sample_min", float(depth_s.min()), "reads", depth_src)
        _add_metric(values, rules, "depth.reads_per_sample_median", float(depth_s.median()), "reads", depth_src)
        _add_metric(values, rules, "depth.reads_per_sample_max", float(depth_s.max()), "reads", depth_src)

    len_dist: dict[str, int] = {}
    len_orig_path = outdir / "asv_length_filter" / "ASV_len_orig.tsv"
    fasta_path = outdir / "dada2" / "ASV_seqs.fasta"
    if len_orig_path.exists():
        l_df = pd.read_csv(len_orig_path, sep="\t")
        l_src = {"path": str(len_orig_path), "sha256": sha256_file(len_orig_path)}
        if not l_df.empty and {"Length", "Counts"}.issubset(l_df.columns):
            lens: list[int] = []
            for _, row in l_df.iterrows():
                ln = int(row["Length"])
                cnt = int(row["Counts"])
                len_dist[str(ln)] = cnt
                lens.extend([ln] * cnt)
            if lens:
                ls = pd.Series(lens)
                _add_metric(values, rules, "asv.count", float(len(lens)), "ASVs", l_src)
                _add_metric(values, rules, "asv.length_min", float(ls.min()), "bp", l_src)
                _add_metric(values, rules, "asv.length_median", float(ls.median()), "bp", l_src)
                _add_metric(values, rules, "asv.length_max", float(ls.max()), "bp", l_src)
    elif fasta_path.exists():
        lens = _fasta_lengths(fasta_path)
        f_src = {"path": str(fasta_path), "sha256": sha256_file(fasta_path)}
        if lens:
            ls = pd.Series(lens)
            for ln, cnt in ls.value_counts().sort_index().items():
                len_dist[str(int(ln))] = int(cnt)
            _add_metric(values, rules, "asv.count", float(len(lens)), "ASVs", f_src)
            _add_metric(values, rules, "asv.length_min", float(ls.min()), "bp", f_src)
            _add_metric(values, rules, "asv.length_median", float(ls.median()), "bp", f_src)
            _add_metric(values, rules, "asv.length_max", float(ls.max()), "bp", f_src)

    tax_path = _find_taxonomy_file(outdir)
    if tax_path is not None:
        t_df = pd.read_csv(tax_path, sep="\t")
        t_src = {"path": str(tax_path), "sha256": sha256_file(tax_path)}
        if not t_df.empty:
            rank_names = ("domain", "kingdom", "phylum", "class", "order", "family", "genus", "species")
            if "Taxon" in t_df.columns:
                split_ranks = t_df["Taxon"].astype(str).str.split(";")
                rank_labels = ["domain", "phylum", "class", "order", "family", "genus", "species"]
                for idx, rk in enumerate(rank_labels):
                    col_vals = split_ranks.apply(lambda xs, i=idx: xs[i].strip() if i < len(xs) else "")
                    pct = float(col_vals.apply(_is_unassigned).mean() * 100.0)
                    _add_metric(values, rules, f"taxonomy.unassigned_{rk}_pct", pct, "%", t_src)
            else:
                low_cols = {str(c).lower(): str(c) for c in t_df.columns}
                for rk in rank_names:
                    if rk in low_cols:
                        pct = float(t_df[low_cols[rk]].apply(_is_unassigned).mean() * 100.0)
                        _add_metric(values, rules, f"taxonomy.unassigned_{rk}_pct", pct, "%", t_src)

    mqc_metrics, _ = _parse_multiqc(outdir / "multiqc" / "multiqc_data")
    for k, (val, unit, src) in mqc_metrics.items():
        _add_metric(values, rules, k, val, unit, src)

    violations = [
        {"metric": k, "value": v["value"], "rule": v["rule"], "result": v["result"], "source": v["source"]}
        for k, v in values.items()
        if v["result"] in {"below", "above"}
    ]
    next_state = "BLOCKED" if violations else "MEASURED"

    metrics_path = ws.metrics_dir / f"{run}.json"
    report = {
        "run": run,
        "generated_at": utc_iso(),
        "rules_source": file_info(ws.rules_file),
        "values": values,
        "samples": samples,
        "asv_length_distribution": len_dist,
        "result": next_state,
    }
    m_sha = write_json_atomic(metrics_path, report)

    ws.ledger.append(
        "metrics_recorded",
        {
            "run": run,
            "metrics": {"path": str(metrics_path), "sha256": m_sha},
            "rules_source": report["rules_source"],
            "result": next_state,
            "violations": violations,
        },
    )
    if violations:
        ws.ledger.append(
            "error",
            {"run": run, "stage": "measure", "reason": "regra excedida", "violations": violations},
        )
    ws.state.move(run, next_state)

    numeric_metrics = {k.replace(".", "_"): v["value"] for k, v in values.items()}
    mlflow_info = log_mlflow_run(
        ws,
        run_name=f"{run}-measured",
        params={"run": run, "state": next_state},
        metrics=numeric_metrics,
        tags={"run": run, "stage": "measure", "denoiser": "dada2"},
        artifacts=[metrics_path],
    )
    return {
        "run": run,
        "state": next_state,
        "metrics_count": len(values),
        "violations": violations,
        "metrics": {"path": str(metrics_path), "sha256": m_sha},
        "mlflow": mlflow_info,
    }
