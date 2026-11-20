"""Stage 5: measure worker reading nf-core/ampliseq 3.1.0 fixture outputs."""

import hashlib
import json
import re

import pytest

from sentinel import measure
from sentinel.cli import main
from sentinel.runs import ConfigError
from sentinel.workspace import Workspace


def _write_ampliseq_fixture(ws: Workspace, run_name: str, *, merged_pct_low: bool = False) -> None:
    out = ws.root / "runs" / run_name / "results"
    (out / "dada2").mkdir(parents=True, exist_ok=True)
    (out / "cutadapt").mkdir(parents=True, exist_ok=True)
    (out / "multiqc" / "multiqc_data").mkdir(parents=True, exist_ok=True)

    merged_vals = ("580", "580") if merged_pct_low else ("800", "850")
    (out / "dada2" / "DADA2_stats.tsv").write_text(
        "sample\tDADA2_input\tfiltered\tdenoisedF\tdenoisedR\tdenoisedPairs\tmerged\tnonchim\n"
        f"SRR1\t1000\t900\t880\t870\t860\t{merged_vals[0]}\t540\n"
        f"SRR2\t1000\t900\t890\t880\t870\t{merged_vals[1]}\t560\n",
        encoding="utf-8",
    )
    (out / "cutadapt" / "cutadapt_summary.tsv").write_text(
        "sample\tcutadapt_total_processed\tcutadapt_reverse_complemented\tcutadapt_passing_filters\tcutadapt_passing_filters_percent\n"
        "SRR1\t1,100\t0\t990\t90.0%\n"
        "SRR2\t1,100\t0\t1,010\t91.8%\n",
        encoding="utf-8",
    )
    (out / "dada2" / "ASV_table.tsv").write_text(
        "ASV_ID\tSRR1\tSRR2\n"
        "asv1\t400\t450\n"
        "asv2\t140\t110\n",
        encoding="utf-8",
    )
    (out / "dada2" / "ASV_seqs.fasta").write_text(
        ">asv1\n" + ("A" * 250) + "\n>asv2\n" + ("C" * 254) + "\n",
        encoding="utf-8",
    )
    (out / "dada2" / "ASV_tax.tsv").write_text(
        "ASV_ID\tKingdom\tPhylum\tClass\tOrder\tFamily\tGenus\tSpecies\n"
        "asv1\tBacteria\tFirmicutes\tBacilli\tLactobacillales\tLactobacillaceae\tLactobacillus\tcrispatus\n"
        "asv2\tBacteria\tFirmicutes\tBacilli\tLactobacillales\tLactobacillaceae\t\t\n",
        encoding="utf-8",
    )
    (out / "multiqc" / "multiqc_data" / "multiqc_general_stats.txt").write_text(
        "Sample\tFastQC_mqc-generalstats-fastqc-percent_gc\tFastQC_mqc-generalstats-fastqc-avg_sequence_length\tFastQC_mqc-generalstats-fastqc-total_sequences\n"
        "SRR1\t52.0\t250.0\t1100\n"
        "SRR2\t50.0\t250.0\t1100\n",
        encoding="utf-8",
    )


def _setup_ws(tmp_path, rules_yaml: str = "", *, merged_pct_low: bool = False) -> Workspace:
    ws = Workspace.create(tmp_path / "ws", name="t")
    ws.runs_file.write_text("runs:\n  P14: {}\n", encoding="utf-8")
    if rules_yaml:
        ws.rules_file.write_text(rules_yaml, encoding="utf-8")
    ws.state.register(["P14"])
    ws.state.move("P14", "FETCHING")
    ws.state.move("P14", "MD5_OK")
    ws.state.move("P14", "PROCESSING")
    _write_ampliseq_fixture(ws, "P14", merged_pct_low=merged_pct_low)
    return ws


def test_measure_empty_rules_produces_no_rule_only_and_reaches_measured(tmp_path):
    ws = _setup_ws(tmp_path)
    res = measure.measure_run(ws, "P14")
    assert res["state"] == "MEASURED"
    assert ws.state.get("P14") == "MEASURED"
    assert res["violations"] == []

    data = json.loads((ws.metrics_dir / "P14.json").read_text(encoding="utf-8"))
    vals = data["values"]
    assert vals["dada2.filtered_pct"]["value"] == 90.0
    assert vals["dada2.merged_pct"]["value"] == 82.5
    assert vals["primers.found_pct"]["value"] == pytest.approx(90.9091, rel=1e-4)
    assert vals["depth.reads_per_sample_min"]["value"] == 540.0
    assert vals["asv.count"]["value"] == 2.0
    assert vals["asv.length_median"]["value"] == 252.0
    assert vals["taxonomy.unassigned_genus_pct"]["value"] == 50.0
    assert vals["quality.fastqc_percent_gc"]["value"] == 51.0

    for item in vals.values():
        assert item["rule"] is None
        assert item["result"] == "no_rule"
        assert len(item["source"]["sha256"]) == 64

    # No interpretive words in metrics JSON (use word boundaries to avoid hex sha256 matches)
    raw_lower = json.dumps(data).lower()
    assert not re.search(r"\b(good|bad|bom|ruim|recommend\w*|recomendo)\b", raw_lower)
    assert ws.ledger.verify().ok


def test_measure_rule_violation_blocks_with_rule_name_and_source_hash(tmp_path):
    rules = "dada2:\n  merged_pct_min: 70\n  filtered_pct_min: 50\ndepth:\n  reads_per_sample_min: 100\n"
    ws = _setup_ws(tmp_path, rules_yaml=rules, merged_pct_low=True)
    res = measure.measure_run(ws, "P14")
    assert res["state"] == "BLOCKED"
    assert ws.state.get("P14") == "BLOCKED"
    assert [v["metric"] for v in res["violations"]] == ["dada2.merged_pct"]

    data = json.loads((ws.metrics_dir / "P14.json").read_text(encoding="utf-8"))
    assert data["values"]["dada2.merged_pct"]["value"] == 58.0
    assert data["values"]["dada2.merged_pct"]["rule"] == "dada2.merged_pct_min"
    assert data["values"]["dada2.merged_pct"]["result"] == "below"
    assert data["values"]["dada2.filtered_pct"]["result"] == "within"
    stats_sha = hashlib.sha256((ws.root / "runs" / "P14" / "results" / "dada2" / "DADA2_stats.tsv").read_bytes()).hexdigest()
    assert data["values"]["dada2.merged_pct"]["source"]["sha256"] == stats_sha
    assert main(["-w", str(ws.root), "verify"]) == 0


def test_measure_refuses_wrong_state_or_invalid_rules(tmp_path):
    ws = Workspace.create(tmp_path / "ws", name="t")
    ws.runs_file.write_text("runs:\n  P1: {}\n", encoding="utf-8")
    ws.state.register(["P1"])
    with pytest.raises(measure.MeasureRefused, match="PENDING"):
        measure.measure_run(ws, "P1")

    ws.state.move("P1", "FETCHING")
    ws.state.move("P1", "MD5_OK")
    ws.state.move("P1", "PROCESSING")
    ws.rules_file.write_text("dada2:\n  unknown_threshold: 10\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="unknown_threshold"):
        measure.measure_run(ws, "P1")


def test_cli_measure_exit_codes(tmp_path):
    ws_ok = _setup_ws(tmp_path / "ok")
    assert main(["-w", str(ws_ok.root), "measure", "P14"]) == 0
    ws_bad = _setup_ws(tmp_path / "bad", rules_yaml="dada2:\n  merged_pct_min: 99\n")
    assert main(["-w", str(ws_bad.root), "measure", "P14"]) == 1
    assert main(["-w", str(ws_bad.root), "measure", "NOPE"]) == 2
