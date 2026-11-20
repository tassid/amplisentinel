"""Validation for researcher-authored rules in config/rules.yaml.

An empty rules.yaml file is valid and yields raw numbers only (`no_rule`).
Unknown groups or keys are refused with the accepted list. AmpliSentinel never
invents or adjusts rule thresholds.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .runs import ConfigError

METRIC_GROUPS: dict[str, tuple[str, ...]] = {
    "dada2": (
        "filtered_pct_min",
        "filtered_pct_max",
        "merged_pct_min",
        "merged_pct_max",
        "nonchim_pct_min",
        "nonchim_pct_max",
        "chimera_pct_min",
        "chimera_pct_max",
    ),
    "primers": (
        "found_pct_min",
        "found_pct_max",
    ),
    "depth": (
        "reads_per_sample_min",
        "reads_per_sample_max",
        "reads_per_sample_median_min",
        "reads_per_sample_median_max",
    ),
    "asv": (
        "count_min",
        "count_max",
        "length_min",
        "length_max",
        "length_median_min",
        "length_median_max",
    ),
    "taxonomy": (
        "unassigned_domain_pct_min",
        "unassigned_domain_pct_max",
        "unassigned_kingdom_pct_min",
        "unassigned_kingdom_pct_max",
        "unassigned_phylum_pct_min",
        "unassigned_phylum_pct_max",
        "unassigned_class_pct_min",
        "unassigned_class_pct_max",
        "unassigned_order_pct_min",
        "unassigned_order_pct_max",
        "unassigned_family_pct_min",
        "unassigned_family_pct_max",
        "unassigned_genus_pct_min",
        "unassigned_genus_pct_max",
        "unassigned_species_pct_min",
        "unassigned_species_pct_max",
    ),
    "quality": (
        "fastqc_total_sequences_mean_min",
        "fastqc_total_sequences_mean_max",
        "fastqc_percent_gc_min",
        "fastqc_percent_gc_max",
        "fastqc_avg_sequence_length_min",
        "fastqc_avg_sequence_length_max",
    ),
}

DECONTAM_KEYS = ("method", "threshold", "notcontaminant_threshold")
DECONTAM_METHODS = ("auto", "frequency", "prevalence", "combined", "minimum", "either", "both")
DEBLUR_KEYS = ("trim_length",)
TOP_GROUPS = tuple(sorted([*METRIC_GROUPS.keys(), "decontam", "deblur"]))


def _check_num(val: object, where: str) -> float:
    if isinstance(val, bool) or not isinstance(val, (int, float)):
        raise ConfigError(f"{where}: esperado número")
    return float(val)


def parse_rules(data: object) -> dict[str, dict[str, Any]]:
    """Validate parsed rules.yaml and return a normalized dict."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("rules.yaml: a raiz precisa ser um mapa de grupos de regras")

    unknown_groups = sorted(str(k) for k in data if k not in TOP_GROUPS)
    if unknown_groups:
        raise ConfigError(
            f"rules.yaml: grupo desconhecido {unknown_groups}; aceitos: {', '.join(TOP_GROUPS)}"
        )

    out: dict[str, dict[str, Any]] = {}
    for group, body in data.items():
        if body is None:
            continue
        if not isinstance(body, dict):
            raise ConfigError(f"rules.yaml {group}: esperado um mapa")

        if group == "decontam":
            bad = sorted(str(k) for k in body if k not in DECONTAM_KEYS)
            if bad:
                raise ConfigError(
                    f"rules.yaml decontam: chave desconhecida {bad}; aceitas: {', '.join(DECONTAM_KEYS)}"
                )
            parsed: dict[str, Any] = {}
            if "method" in body and body["method"] is not None:
                m = body["method"]
                if not isinstance(m, str) or m not in DECONTAM_METHODS:
                    raise ConfigError(
                        f"rules.yaml decontam.method: aceitos {', '.join(DECONTAM_METHODS)}"
                    )
                parsed["method"] = m
            for tk in ("threshold", "notcontaminant_threshold"):
                if tk in body and body[tk] is not None:
                    v = _check_num(body[tk], f"rules.yaml decontam.{tk}")
                    if not (0.0 < v <= 1.0):
                        raise ConfigError(f"rules.yaml decontam.{tk}: esperado número entre 0 e 1")
                    parsed[tk] = v
            if parsed:
                out["decontam"] = parsed
            continue

        if group == "deblur":
            bad = sorted(str(k) for k in body if k not in DEBLUR_KEYS)
            if bad:
                raise ConfigError(
                    f"rules.yaml deblur: chave desconhecida {bad}; aceitas: {', '.join(DEBLUR_KEYS)}"
                )
            parsed = {}
            if "trim_length" in body and body["trim_length"] is not None:
                tl = body["trim_length"]
                if isinstance(tl, bool) or not isinstance(tl, int) or tl < -1:
                    raise ConfigError("rules.yaml deblur.trim_length: esperado inteiro >= -1")
                parsed["trim_length"] = tl
            if parsed:
                out["deblur"] = parsed
            continue

        accepted = METRIC_GROUPS[group]
        bad = sorted(str(k) for k in body if k not in accepted)
        if bad:
            raise ConfigError(
                f"rules.yaml {group}: chave desconhecida {bad}; aceitas: {', '.join(accepted)}"
            )
        parsed = {}
        for k, v in body.items():
            if v is not None:
                parsed[k] = _check_num(v, f"rules.yaml {group}.{k}")
        if parsed:
            out[group] = parsed
    return out


def load_rules(path: Path) -> dict[str, dict[str, Any]]:
    """Read and validate config/rules.yaml. A missing or empty file returns {}."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"rules.yaml não é YAML válido: {exc}") from None
    return parse_rules(data)
