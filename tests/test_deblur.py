"""Stage 3b: optional Deblur denoiser via pinned QIIME 2 container."""

import hashlib
import json
from pathlib import Path

import pytest

from sentinel import deblur, run
from sentinel.cli import main
from sentinel.ext import CallResult
from sentinel.rules import ConfigError, parse_rules
from tests.test_run import FakeRunTools, make_ready_ws

QIIME_DIGEST_IMAGE = "quay.io/qiime2/amplicon:2024.10@sha256:" + "c" * 64


class FakeDeblurTools:
    """Fake docker/podman running QIIME 2 import, cutadapt, and deblur."""

    def __init__(self, fail_step: str | None = None):
        self.fail_step = fail_step
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CallResult:
        self.calls.append(list(argv))
        if argv[1] in ("version", "--version"):
            return CallResult(0, b"docker version 27.3.1\n", b"")
        if self.fail_step and self.fail_step in argv:
            return CallResult(1, b"", f"qiime error in {self.fail_step}\n".encode())
        for flag in ("--output-path", "--o-trimmed-sequences", "--o-table", "--o-representative-sequences", "--o-stats"):
            if flag in argv:
                out = Path(argv[argv.index(flag) + 1])
                out.parent.mkdir(parents=True, exist_ok=True)
                out.write_bytes(f"qza:{out.name}\n".encode())
        return CallResult(0, b"Saved FeatureTable\n", b"")


def test_deblur_creates_isolated_experiment_without_touching_dada2(tmp_path):
    ws = make_ready_ws(
        tmp_path,
        meta_extra=f"run:\n  qiime2_image: '{QIIME_DIGEST_IMAGE}'\n",
    )
    (ws.rules_file).write_text("deblur:\n  trim_length: 200\n", encoding="utf-8")

    # First run DADA2 (Stage 3)
    dada2_res = run.run_ampliseq(ws, "S1", runner=FakeRunTools(), environ={})
    assert dada2_res["state"] == "PROCESSING"
    dada2_manifest = Path(dada2_res["manifest"]["path"])
    dada2_sha_before = hashlib.sha256(dada2_manifest.read_bytes()).hexdigest()
    (ws.root / "runs" / "S1" / "results" / "marker.txt").write_text("dada2", encoding="utf-8")

    # Then run Deblur (Stage 3b)
    tools = FakeDeblurTools()
    deb_res = deblur.run_deblur(ws, "S1", runner=tools)
    assert deb_res["status"] == "COMPLETED"
    assert deb_res["denoiser"] == "deblur"
    assert deb_res["experiment_id"].startswith("S1-deblur-")
    assert set(deb_res["artifacts"]) >= {"demux", "trimmed", "table", "rep_seqs", "stats"}

    # DADA2 state, manifest, and results remain untouched
    assert ws.state.get("S1") == "PROCESSING"
    assert hashlib.sha256(dada2_manifest.read_bytes()).hexdigest() == dada2_sha_before
    assert (ws.root / "runs" / "S1" / "results" / "marker.txt").read_text(encoding="utf-8") == "dada2"

    # Separate ledger entry and side-by-side MLflow runs if mlflow is installed
    exp_events = [e for e in ws.ledger.entries() if e["event"] == "experiment_recorded"]
    assert len(exp_events) == 1
    assert exp_events[0]["data"]["experiment_id"] == deb_res["experiment_id"]
    assert exp_events[0]["data"]["qiime2_image"] == QIIME_DIGEST_IMAGE
    assert exp_events[0]["data"]["parameters"]["trim_length"] == 200
    assert ws.ledger.verify().ok

    mlflow = pytest.importorskip("mlflow")
    mlflow.set_tracking_uri(deb_res["mlflow"]["tracking_uri"])
    df = mlflow.search_runs(experiment_names=[ws.name])
    assert set(df["tags.denoiser"]) == {"dada2", "deblur"}


def test_deblur_requires_pinned_image_digest_and_trim_length(tmp_path):
    ws_no_img = make_ready_ws(tmp_path / "a")
    with pytest.raises(run.RunRefused, match="qiime2_image"):
        deblur.run_deblur(ws_no_img, "S1", trim_length=200, runner=FakeDeblurTools())

    ws_unpinned = make_ready_ws(tmp_path / "b", meta_extra="run:\n  qiime2_image: 'quay.io/qiime2/amplicon:2024.10'\n")
    with pytest.raises(run.RunRefused, match="sha256"):
        deblur.run_deblur(ws_unpinned, "S1", trim_length=200, runner=FakeDeblurTools())

    ws_no_trim = make_ready_ws(tmp_path / "c", meta_extra=f"run:\n  qiime2_image: '{QIIME_DIGEST_IMAGE}'\n")
    with pytest.raises(run.RunRefused, match="trim_length"):
        deblur.run_deblur(ws_no_trim, "S1", runner=FakeDeblurTools())


def test_deblur_failure_records_literal_error_without_altering_dada2_state(tmp_path):
    ws = make_ready_ws(
        tmp_path,
        meta_extra=f"run:\n  qiime2_image: '{QIIME_DIGEST_IMAGE}'\n",
        state="MD5_OK",
    )
    res = deblur.run_deblur(ws, "S1", trim_length=150, runner=FakeDeblurTools(fail_step="denoise-16S"))
    assert res["status"] == "FAILED"
    assert ws.state.get("S1") == "MD5_OK"
    err = [e for e in ws.ledger.entries() if e["event"] == "error"][-1]
    assert Path(err["data"]["stderr"]["path"]).read_bytes() == b"qiime error in denoise-16S\n"
    assert ws.ledger.verify().ok


def test_cli_run_deblur(tmp_path, monkeypatch):
    ws = make_ready_ws(tmp_path, meta_extra=f"run:\n  qiime2_image: '{QIIME_DIGEST_IMAGE}'\n")
    tools = FakeDeblurTools()
    original = deblur.run_deblur
    monkeypatch.setattr(deblur, "run_deblur", lambda ws_, r, **kw: original(ws_, r, runner=tools, **kw))
    assert main(["-w", str(ws.root), "run", "S1", "--denoiser", "deblur", "--trim-length", "200"]) == 0
    exp_dir = next((ws.root / "runs" / "S1" / "experiments").iterdir())
    exp = json.loads((exp_dir / "experiment.json").read_text(encoding="utf-8"))
    assert exp["parameters"]["trim_length"] == 200


def test_rules_rejects_unknown_keys():
    assert parse_rules(None) == {}
    with pytest.raises(ConfigError, match="grupo desconhecido"):
        parse_rules({"unknown": {"x": 1}})
    with pytest.raises(ConfigError, match="chave desconhecida"):
        parse_rules({"dada2": {"bad_key": 10}})
