"""Stage 5b: optional contaminant detection with decontam."""

import hashlib
from pathlib import Path

from amplisentinel import decontam
from amplisentinel.cli import main
from amplisentinel.ext import CallResult
from amplisentinel.runs import parse_runs
from amplisentinel.workspace import Workspace

DECONTAM_IMAGE = "community.wave.seqera.io/library/bioconductor-decontam_r-base@sha256:" + "d" * 64


class FakeDecontamTools:
    def __call__(self, argv: list[str]) -> CallResult:
        if argv[1] in ("version", "--version"):
            return CallResult(0, b"docker version 27.3.1\n", b"")
        script_path = Path(argv[-1])
        dec_dir = script_path.parent
        (dec_dir / "decontaminated.tsv").write_text(
            "ASV_ID\tSRR1\tNEG1\nasv1\t400\t0\n",
            encoding="utf-8",
        )
        (dec_dir / "decontaminated_details.tsv").write_text(
            "ASV_ID\tfreq\tprev\tp.freq\tp.prev\tp\tcontaminant\n"
            "asv1\t400\t0.5\tNA\t0.85\t0.85\tFALSE\n"
            "asv2\t20\t0.5\tNA\t0.02\t0.02\tTRUE\n",
            encoding="utf-8",
        )
        return CallResult(0, b"", b"")


def _setup(tmp_path: Path, *, with_controls: bool = True, with_rules: bool = True) -> Workspace:
    ws = Workspace.create(tmp_path / "ws", name="t")
    (ws.root / "amplisentinel.yaml").write_text(
        f"name: t\ntitle: t\nrun:\n  decontam_image: '{DECONTAM_IMAGE}'\n",
        encoding="utf-8",
    )
    ctrl = "\n    negative_controls: [NEG1]" if with_controls else ""
    ws.runs_file.write_text(f"runs:\n  P1:{{}}{ctrl}\n".replace("P1:{}", "P1:\n    region: V4") if ctrl else "runs:\n  P1: {}\n", encoding="utf-8")
    if with_rules:
        ws.rules_file.write_text("decontam:\n  method: prevalence\n  threshold: 0.1\n", encoding="utf-8")
    ws.state.register(["P1"])
    ws.state.move("P1", "FETCHING")
    ws.state.move("P1", "MD5_OK")
    ws.state.move("P1", "PROCESSING")
    ws.state.move("P1", "MEASURED")
    dada2_dir = ws.root / "runs" / "P1" / "results" / "dada2"
    dada2_dir.mkdir(parents=True, exist_ok=True)
    (dada2_dir / "ASV_table.tsv").write_text(
        "ASV_ID\tSRR1\tNEG1\nasv1\t400\t0\nasv2\t10\t50\n",
        encoding="utf-8",
    )
    return ws


def test_without_negative_controls_nothing_happens(tmp_path):
    ws = _setup(tmp_path, with_controls=False, with_rules=False)
    before_entries = len(ws.ledger.entries())
    res = decontam.run_decontam(ws, "P1", runner=FakeDecontamTools())
    assert res["status"] == "skipped"
    assert ws.state.get("P1") == "MEASURED"
    assert len(ws.ledger.entries()) == before_entries


def test_missing_decontam_rules_blocks_and_asks(tmp_path):
    ws = _setup(tmp_path, with_controls=True, with_rules=False)
    res = decontam.run_decontam(ws, "P1", runner=FakeDecontamTools())
    assert res["status"] == "BLOCKED"
    assert ws.state.get("P1") == "BLOCKED"
    err = [e for e in ws.ledger.entries() if e["event"] == "error"][-1]
    assert "decontam.method" in err["data"]["reason"]
    assert ws.ledger.verify().ok


def test_decontam_records_flagged_asvs_scores_and_table_hashes(tmp_path):
    ws = _setup(tmp_path, with_controls=True, with_rules=True)
    res = decontam.run_decontam(ws, "P1", runner=FakeDecontamTools())
    assert res["status"] == "COMPLETED"
    assert res["flagged_asvs"] == [{"asv_id": "asv2", "score": 0.02}]

    before_sha = hashlib.sha256((ws.root / "runs" / "P1" / "results" / "dada2" / "ASV_table.tsv").read_bytes()).hexdigest()
    after_sha = hashlib.sha256((ws.root / "runs" / "P1" / "decontam" / "decontaminated.tsv").read_bytes()).hexdigest()
    assert res["table_before"]["sha256"] == before_sha
    assert res["table_after"]["sha256"] == after_sha

    ev = [e for e in ws.ledger.entries() if e["event"] == "decontam_recorded"][-1]
    assert ev["data"]["flagged_asvs"] == [{"asv_id": "asv2", "score": 0.02}]
    assert ev["data"]["table_before"]["sha256"] == before_sha
    assert ev["data"]["table_after"]["sha256"] == after_sha
    assert ws.ledger.verify().ok


def test_cli_decontam_and_runs_negative_controls(tmp_path):
    parsed = parse_runs({"runs": {"P1": {"negative_controls": ["NEG1", "NEG2"]}}})
    assert parsed["P1"].negative_controls == ("NEG1", "NEG2")
    ws = _setup(tmp_path, with_controls=False, with_rules=False)
    assert main(["-w", str(ws.root), "decontam", "P1"]) == 0
