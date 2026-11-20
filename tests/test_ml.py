"""Stage 9: ML bridge (export with .qza UUID, deterministic LOPO folds, RF/t-SNE/SHAP)."""

import hashlib
import zipfile
from pathlib import Path

import pytest

from amplisentinel import ml
from amplisentinel.cli import main
from amplisentinel.workspace import Workspace


def _make_qza(path: Path, uuid: str) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr(f"{uuid}/metadata.yaml", f"uuid: {uuid}\ntype: FeatureTable[Frequency]\n")


def _write_ml_inputs(tmp_path: Path) -> tuple[Path, Path, Path]:
    qza = tmp_path / "merged-table.qza"
    _make_qza(qza, "11111111-2222-3333-4444-555555555555")

    table = tmp_path / "feature-table.tsv"
    table.write_text(
        "ASV_ID\tS1\tS2\tS3\tS4\tS5\tS6\n"
        "asv1\t100\t90\t5\t95\t12\t8\n"
        "asv2\t10\t12\t110\t8\t98\t105\n"
        "asv3\t30\t25\t28\t32\t22\t20\n",
        encoding="utf-8",
    )
    meta = tmp_path / "metadata.tsv"
    meta.write_text(
        "sample\tstudy\tdiagnosis\n"
        "S1\tP1\tcontrol\n"
        "S2\tP1\tcontrol\n"
        "S3\tP1\tcase\n"
        "S4\tP2\tcontrol\n"
        "S5\tP2\tcase\n"
        "S6\tP2\tcase\n",
        encoding="utf-8",
    )
    return qza, table, meta


def test_export_folds_identical_and_qza_uuid_in_ledger_and_mlflow(tmp_path):
    qza, table, meta = _write_ml_inputs(tmp_path)
    ws = Workspace.create(tmp_path / "ws", name="cervical")
    (ws.root / "config" / "ml.yaml").write_text(
        "seed: 42\n"
        "target_column: diagnosis\n"
        "study_column: study\n"
        "random_forest:\n"
        "  n_estimators: 10\n"
        "  max_depth: 3\n"
        "tsne:\n"
        "  perplexity: 2.0\n"
        "  max_iter: 250\n",
        encoding="utf-8",
    )

    exp = ml.export_features(ws, qza_path=qza, table_path=table)
    assert exp["source_qza"]["uuid"] == "11111111-2222-3333-4444-555555555555"
    assert Path(exp["tsv"]["path"]).exists()
    assert Path(exp["parquet"]["path"]).exists()

    f1 = ml.build_folds(ws, metadata_path=meta)
    bytes1 = Path(f1["folds_file"]["path"]).read_bytes()
    f2 = ml.build_folds(ws, metadata_path=meta)
    bytes2 = Path(f2["folds_file"]["path"]).read_bytes()
    assert bytes1 == bytes2
    assert hashlib.sha256(bytes1).hexdigest() == f1["folds_file"]["sha256"]
    assert f1["folds"][0]["train_class_counts"] == {"case": 2, "control": 1}

    res = ml.train_models(ws, metadata_path=meta)
    assert res["source_qza_uuid"] == "11111111-2222-3333-4444-555555555555"
    assert Path(res["artifacts"]["shap_values"]["path"]).exists()
    assert Path(res["artifacts"]["tsne_coords"]["path"]).exists()

    trained_ev = [e for e in ws.ledger.entries() if e["event"] == "ml_trained"][-1]
    assert trained_ev["data"]["source_qza_uuid"] == "11111111-2222-3333-4444-555555555555"
    assert trained_ev["data"]["folds_sha256"] == f1["folds_file"]["sha256"]
    assert ws.ledger.verify().ok

    mlflow = pytest.importorskip("mlflow")
    mlflow.set_tracking_uri(res["mlflow"]["tracking_uri"])
    run_obj = mlflow.get_run(res["mlflow"]["run_id"])
    assert run_obj.data.params["source_qza_uuid"] == "11111111-2222-3333-4444-555555555555"
    assert run_obj.data.params["folds_sha256"] == f1["folds_file"]["sha256"]
    assert run_obj.data.tags["source_qza_uuid"] == "11111111-2222-3333-4444-555555555555"


def test_train_refuses_without_researcher_ml_yaml(tmp_path):
    ws = Workspace.create(tmp_path / "ws", name="cervical")
    meta = tmp_path / "m.tsv"
    meta.write_text("sample\tstudy\tdiagnosis\n", encoding="utf-8")
    with pytest.raises(ml.MLRefused, match="hiperparâmetros"):
        ml.train_models(ws, metadata_path=meta)


def test_cli_export_folds_train(tmp_path):
    qza, table, meta = _write_ml_inputs(tmp_path)
    ws = Workspace.create(tmp_path / "ws", name="cervical")
    (ws.root / "config" / "ml.yaml").write_text(
        "seed: 7\ntarget_column: diagnosis\nrandom_forest:\n  n_estimators: 5\ntsne:\n  perplexity: 2.0\n  max_iter: 250\n",
        encoding="utf-8",
    )
    assert main(["-w", str(ws.root), "export", "--qza", str(qza), "--table", str(table)]) == 0
    assert main(["-w", str(ws.root), "folds", "--metadata", str(meta)]) == 0
    assert main(["-w", str(ws.root), "train", "--metadata", str(meta)]) == 0
    assert main(["-w", str(ws.root), "verify"]) == 0
