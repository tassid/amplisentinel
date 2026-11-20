"""Stage 9: ML bridge (export with .qza UUID, deterministic LOPO folds, RF/t-SNE/SHAP).

Commands:
- `amplisentinel export`: exports merged feature table to TSV and Parquet using pandas
  and records the source `.qza` UUID in the ledger.
- `amplisentinel folds`: builds deterministic leave-one-project-out (LOPO) folds with
  per-class sample counts and hashes.
- `amplisentinel train`: runs Random Forest, t-SNE (scikit-learn), and SHAP from a
  researcher-written hyperparameter file (`config/ml.yaml`) and records seed,
  library versions, folds hash, and `.qza` UUID in both the ledger and MLflow.
"""

from __future__ import annotations

import json
import platform
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
import yaml

from .common import log_mlflow_run, sha256_file, utc_iso, write_json_atomic
from .runs import ConfigError
from .workspace import Workspace

ML_TOP_KEYS = ("seed", "target_column", "study_column", "random_forest", "tsne")
RF_KEYS = ("n_estimators", "max_depth", "min_samples_split", "min_samples_leaf", "max_features", "class_weight")
TSNE_KEYS = ("perplexity", "max_iter", "learning_rate", "init", "metric")


class MLRefused(Exception):
    """ML bridge step could not start."""


def extract_qza_uuid(qza_path: Path) -> str:
    """Read the provenance UUID from `<uuid>/metadata.yaml` inside a QIIME 2 .qza."""
    if not qza_path.exists():
        raise MLRefused(f"arquivo .qza não encontrado: {qza_path}")
    try:
        with zipfile.ZipFile(qza_path, "r") as zf:
            meta_names = [n for n in zf.namelist() if n.endswith("/metadata.yaml") and n.count("/") == 1]
            if not meta_names:
                raise MLRefused(f"arquivo .qza inválido (falta metadata.yaml): {qza_path}")
            meta = yaml.safe_load(zf.read(meta_names[0]).decode("utf-8")) or {}
    except (zipfile.BadZipFile, yaml.YAMLError) as exc:
        raise MLRefused(f"arquivo .qza inválido ({exc}): {qza_path}") from None
    uuid = meta.get("uuid")
    if not isinstance(uuid, str) or not uuid.strip():
        raise MLRefused(f"arquivo .qza sem campo uuid em metadata.yaml: {qza_path}")
    return uuid.strip()


def load_ml_config(path: Path) -> dict[str, Any]:
    """Validate researcher-written ML hyperparameters (`config/ml.yaml`)."""
    if not path.exists():
        raise MLRefused(f"falta arquivo de hiperparâmetros escrito pela pesquisadora: {path}")
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"ml.yaml não é YAML válido: {exc}") from None
    if not isinstance(data, dict) or not data:
        raise MLRefused(f"{path}: preencha seed, target_column, random_forest e tsne")

    unknown = sorted(str(k) for k in data if k not in ML_TOP_KEYS)
    if unknown:
        raise ConfigError(f"ml.yaml: chave desconhecida {unknown}; aceitas: {', '.join(ML_TOP_KEYS)}")

    for req in ("seed", "target_column", "random_forest", "tsne"):
        if req not in data or data[req] is None:
            raise MLRefused(f"ml.yaml: falta chave obrigatória '{req}'")

    seed = data["seed"]
    if isinstance(seed, bool) or not isinstance(seed, int):
        raise ConfigError("ml.yaml seed: esperado inteiro")
    target = data["target_column"]
    if not isinstance(target, str) or not target.strip():
        raise ConfigError("ml.yaml target_column: esperado texto")
    study = data.get("study_column", "study")
    if not isinstance(study, str) or not study.strip():
        raise ConfigError("ml.yaml study_column: esperado texto")

    rf = data["random_forest"]
    if not isinstance(rf, dict) or not rf:
        raise ConfigError("ml.yaml random_forest: esperado mapa de hiperparâmetros")
    bad_rf = sorted(str(k) for k in rf if k not in RF_KEYS)
    if bad_rf:
        raise ConfigError(f"ml.yaml random_forest: chave desconhecida {bad_rf}; aceitas: {', '.join(RF_KEYS)}")

    tsne = data["tsne"]
    if not isinstance(tsne, dict) or not tsne:
        raise ConfigError("ml.yaml tsne: esperado mapa de hiperparâmetros")
    bad_tsne = sorted(str(k) for k in tsne if k not in TSNE_KEYS)
    if bad_tsne:
        raise ConfigError(f"ml.yaml tsne: chave desconhecida {bad_tsne}; aceitas: {', '.join(TSNE_KEYS)}")

    return {
        "seed": seed,
        "target_column": target.strip(),
        "study_column": study.strip(),
        "random_forest": dict(rf),
        "tsne": dict(tsne),
    }


def export_features(
    ws: Workspace,
    *,
    qza_path: Path,
    table_path: Path,
) -> dict[str, Any]:
    """Export feature table to TSV and Parquet with pandas, logging the .qza UUID."""
    uuid = extract_qza_uuid(qza_path)
    if not table_path.exists():
        raise MLRefused(f"tabela de entrada não encontrada: {table_path}")

    df = pd.read_csv(table_path, sep="\t")
    if df.empty or len(df.columns) < 2:
        raise MLRefused(f"tabela vazia ou inválida: {table_path}")

    ml_dir = ws.home / "ml"
    ml_dir.mkdir(parents=True, exist_ok=True)
    tsv_path = ml_dir / "features.tsv"
    pq_path = ml_dir / "features.parquet"

    tmp_tsv = tsv_path.with_suffix(".tsv.tmp")
    df.to_csv(tmp_tsv, sep="\t", index=False)
    tmp_tsv.replace(tsv_path)

    tmp_pq = pq_path.with_suffix(".parquet.tmp")
    df.to_parquet(tmp_pq, index=False)
    tmp_pq.replace(pq_path)

    manifest_path = ml_dir / "export_manifest.json"
    manifest = {
        "workspace": ws.name,
        "source_qza": {"path": str(qza_path), "sha256": sha256_file(qza_path), "uuid": uuid},
        "source_table": {"path": str(table_path), "sha256": sha256_file(table_path)},
        "tsv": {"path": str(tsv_path), "sha256": sha256_file(tsv_path)},
        "parquet": {"path": str(pq_path), "sha256": sha256_file(pq_path)},
        "rows": int(df.shape[0]),
        "columns": int(df.shape[1]),
    }
    m_sha = write_json_atomic(manifest_path, manifest)

    ws.ledger.append(
        "export",
        {
            "run": "ml",
            "source_qza_uuid": uuid,
            "source_qza": manifest["source_qza"],
            "tsv": manifest["tsv"],
            "parquet": manifest["parquet"],
            "rows": manifest["rows"],
            "columns": manifest["columns"],
            "manifest": {"path": str(manifest_path), "sha256": m_sha},
        },
    )
    return manifest


def build_folds(
    ws: Workspace,
    *,
    metadata_path: Path,
    study_col: str = "study",
    target_col: str = "diagnosis",
) -> dict[str, Any]:
    """Generate deterministic leave-one-project-out folds and class counts."""
    if not metadata_path.exists():
        raise MLRefused(f"arquivo de metadados não encontrado: {metadata_path}")
    meta = pd.read_csv(metadata_path, sep="\t")
    if meta.empty:
        raise MLRefused("metadados vazios")
    id_col = next((c for c in ("sample", "ID", "sample_id") if c in meta.columns), meta.columns[0])
    for col in (study_col, target_col):
        if col not in meta.columns:
            raise MLRefused(f"coluna '{col}' ausente em {metadata_path}")

    meta[id_col] = meta[id_col].astype(str)
    meta[study_col] = meta[study_col].astype(str)
    meta[target_col] = meta[target_col].astype(str)
    meta = meta.sort_values(id_col).reset_index(drop=True)

    projects = sorted(meta[study_col].unique().tolist())
    if len(projects) < 2:
        raise MLRefused("leave-one-project-out exige pelo menos 2 estudos distintos")

    def class_counts(sub: pd.DataFrame) -> dict[str, int]:
        counts = sub[target_col].value_counts().to_dict()
        return {str(k): int(counts[k]) for k in sorted(counts)}

    folds: list[dict[str, Any]] = []
    for idx, proj in enumerate(projects):
        test_df = meta[meta[study_col] == proj]
        train_df = meta[meta[study_col] != proj]
        folds.append(
            {
                "fold": idx,
                "test_project": proj,
                "train_samples": train_df[id_col].tolist(),
                "test_samples": test_df[id_col].tolist(),
                "train_count": len(train_df),
                "test_count": len(test_df),
                "train_class_counts": class_counts(train_df),
                "test_class_counts": class_counts(test_df),
            }
        )

    ml_dir = ws.home / "ml"
    export_manifest = ml_dir / "export_manifest.json"
    source_uuid = None
    if export_manifest.exists():
        source_uuid = (json.loads(export_manifest.read_text(encoding="utf-8")).get("source_qza") or {}).get("uuid")

    folds_path = ml_dir / "folds.json"
    folds_payload = {
        "workspace": ws.name,
        "scheme": "leave-one-project-out",
        "study_column": study_col,
        "target_column": target_col,
        "source_qza_uuid": source_uuid,
        "metadata": {"path": str(metadata_path), "sha256": sha256_file(metadata_path)},
        "folds": folds,
    }
    f_sha = write_json_atomic(folds_path, folds_payload)

    ws.ledger.append(
        "folds",
        {
            "run": "ml",
            "scheme": "leave-one-project-out",
            "n_folds": len(folds),
            "source_qza_uuid": source_uuid,
            "folds_file": {"path": str(folds_path), "sha256": f_sha},
            "folds": [
                {
                    "fold": f["fold"],
                    "test_project": f["test_project"],
                    "train_count": f["train_count"],
                    "test_count": f["test_count"],
                    "train_class_counts": f["train_class_counts"],
                    "test_class_counts": f["test_class_counts"],
                }
                for f in folds
            ],
        },
    )
    return {"folds_file": {"path": str(folds_path), "sha256": f_sha}, **folds_payload}


def _load_feature_matrix(tsv_path: Path, sample_ids: list[str]) -> pd.DataFrame:
    df = pd.read_csv(tsv_path, sep="\t", index_col=0)
    df.index = df.index.astype(str)
    df.columns = df.columns.astype(str)
    col_hits = [s for s in sample_ids if s in df.columns]
    row_hits = [s for s in sample_ids if s in df.index]
    if len(col_hits) >= len(row_hits):
        return df[col_hits].T.astype(float)
    return df.loc[row_hits].astype(float)


def train_models(
    ws: Workspace,
    *,
    metadata_path: Path,
    config_path: Path | None = None,
) -> dict[str, Any]:
    """Train Random Forest and t-SNE with SHAP values and log to MLflow + ledger."""
    import shap
    import sklearn
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.manifold import TSNE
    from sklearn.metrics import accuracy_score, balanced_accuracy_score

    cfg = load_ml_config(config_path or (ws.root / "config" / "ml.yaml"))
    ml_dir = ws.home / "ml"
    features_path = ml_dir / "features.tsv"
    folds_path = ml_dir / "folds.json"
    export_manifest = ml_dir / "export_manifest.json"

    for req in (features_path, folds_path, export_manifest):
        if not req.exists():
            raise MLRefused(f"falta arquivo gerado nas etapas anteriores: {req}")
    if not metadata_path.exists():
        raise MLRefused(f"arquivo de metadados não encontrado: {metadata_path}")

    exp_data = json.loads(export_manifest.read_text(encoding="utf-8"))
    source_uuid = (exp_data.get("source_qza") or {}).get("uuid")
    if not source_uuid:
        raise MLRefused("export_manifest.json sem source_qza.uuid")

    folds_data = json.loads(folds_path.read_text(encoding="utf-8"))
    folds_sha = sha256_file(folds_path)

    meta = pd.read_csv(metadata_path, sep="\t")
    id_col = next((c for c in ("sample", "ID", "sample_id") if c in meta.columns), meta.columns[0])
    meta[id_col] = meta[id_col].astype(str)
    meta = meta.set_index(id_col)

    x_all = _load_feature_matrix(features_path, list(meta.index))
    y_all = meta.loc[x_all.index, cfg["target_column"]].astype(str)
    seed = cfg["seed"]

    fold_metrics: list[dict[str, Any]] = []
    accs: list[float] = []
    baccs: list[float] = []
    for f in folds_data["folds"]:
        tr = [s for s in f["train_samples"] if s in x_all.index]
        te = [s for s in f["test_samples"] if s in x_all.index]
        if not tr or not te:
            continue
        clf = RandomForestClassifier(random_state=seed, **cfg["random_forest"])
        clf.fit(x_all.loc[tr], y_all.loc[tr])
        preds = clf.predict(x_all.loc[te])
        acc = float(accuracy_score(y_all.loc[te], preds))
        bacc = float(balanced_accuracy_score(y_all.loc[te], preds))
        accs.append(acc)
        baccs.append(bacc)
        fold_metrics.append({"fold": f["fold"], "test_project": f["test_project"], "accuracy": acc, "balanced_accuracy": bacc})

    full_rf = RandomForestClassifier(random_state=seed, **cfg["random_forest"])
    full_rf.fit(x_all, y_all)
    explainer = shap.TreeExplainer(full_rf)
    raw_shap = explainer.shap_values(x_all)
    if isinstance(raw_shap, list):
        shap_arr = np.mean([np.abs(np.asarray(s)) for s in raw_shap], axis=0)
    else:
        arr = np.asarray(raw_shap)
        shap_arr = np.mean(np.abs(arr), axis=2) if arr.ndim == 3 else np.abs(arr)
    shap_df = pd.DataFrame(shap_arr, index=x_all.index, columns=x_all.columns)
    shap_path = ml_dir / "shap_values.tsv"
    shap_df.to_csv(shap_path, sep="\t")

    tsne = TSNE(random_state=seed, **cfg["tsne"])
    coords = tsne.fit_transform(x_all.to_numpy())
    tsne_df = pd.DataFrame(coords, index=x_all.index, columns=[f"TSNE{i + 1}" for i in range(coords.shape[1])])
    tsne_path = ml_dir / "tsne_coords.tsv"
    tsne_df.to_csv(tsne_path, sep="\t")

    versions = {
        "python": platform.python_version(),
        "pandas": pd.__version__,
        "scikit_learn": sklearn.__version__,
        "shap": shap.__version__,
    }
    mean_acc = float(np.mean(accs)) if accs else 0.0
    mean_bacc = float(np.mean(baccs)) if baccs else 0.0

    params = {
        "seed": seed,
        "source_qza_uuid": source_uuid,
        "folds_sha256": folds_sha,
        "target_column": cfg["target_column"],
        **{f"version_{k}": v for k, v in versions.items()},
        **{f"rf_{k}": v for k, v in cfg["random_forest"].items()},
        **{f"tsne_{k}": v for k, v in cfg["tsne"].items()},
    }
    metrics = {
        "lopo_accuracy_mean": mean_acc,
        "lopo_balanced_accuracy_mean": mean_bacc,
        **{f"fold_{m['fold']}_accuracy": m["accuracy"] for m in fold_metrics},
    }
    mlflow_info = log_mlflow_run(
        ws,
        run_name=f"{ws.name}-lopo-rf-seed-{seed}",
        params=params,
        metrics=metrics,
        tags={
            "stage": "ml",
            "source_qza_uuid": source_uuid,
            "folds_sha256": folds_sha,
        },
        artifacts=[shap_path, tsne_path, folds_path],
    )

    train_manifest_path = ml_dir / "train_manifest.json"
    manifest = {
        "workspace": ws.name,
        "generated_at": utc_iso(),
        "seed": seed,
        "source_qza_uuid": source_uuid,
        "folds_sha256": folds_sha,
        "versions": versions,
        "hyperparameters": cfg,
        "metrics": {"lopo_accuracy_mean": mean_acc, "lopo_balanced_accuracy_mean": mean_bacc, "folds": fold_metrics},
        "artifacts": {
            "shap_values": {"path": str(shap_path), "sha256": sha256_file(shap_path)},
            "tsne_coords": {"path": str(tsne_path), "sha256": sha256_file(tsne_path)},
        },
        "mlflow": mlflow_info,
    }
    t_sha = write_json_atomic(train_manifest_path, manifest)

    ws.ledger.append(
        "ml_trained",
        {
            "run": "ml",
            "seed": seed,
            "source_qza_uuid": source_uuid,
            "folds_sha256": folds_sha,
            "versions": versions,
            "metrics": manifest["metrics"],
            "artifacts": manifest["artifacts"],
            "mlflow": mlflow_info,
            "manifest": {"path": str(train_manifest_path), "sha256": t_sha},
        },
    )
    return manifest
