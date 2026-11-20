"""Stage 8: PERMANOVA between studies and diagnoses using scikit-bio."""

import json
import re
from pathlib import Path

from sentinel import between
from sentinel.cli import main
from sentinel.workspace import Workspace


def _write_between_inputs(tmp_path: Path) -> tuple[Path, Path]:
    table = tmp_path / "merged_table.tsv"
    table.write_text(
        "ASV_ID\tS1\tS2\tS3\tS4\tS5\tS6\n"
        "asv1\t100\t95\t90\t5\t10\t8\n"
        "asv2\t5\t8\t10\t100\t92\t88\n"
        "asv3\t20\t22\t18\t30\t28\t35\n",
        encoding="utf-8",
    )
    meta = tmp_path / "metadata.tsv"
    meta.write_text(
        "sample\tstudy\tdiagnosis\n"
        "S1\tP1\tcontrol\n"
        "S2\tP1\tcontrol\n"
        "S3\tP1\tcase\n"
        "S4\tP2\tcase\n"
        "S5\tP2\tcase\n"
        "S6\tP2\tcontrol\n",
        encoding="utf-8",
    )
    return table, meta


def test_between_fixed_seed_reproduces_exact_numbers_and_no_opinions(tmp_path):
    table, meta = _write_between_inputs(tmp_path)
    ws1 = Workspace.create(tmp_path / "ws1", name="t")
    ws2 = Workspace.create(tmp_path / "ws2", name="t")

    r1 = between.run_between(ws1, metadata_path=meta, table_path=table, permutations=99, seed=123)
    r2 = between.run_between(ws2, metadata_path=meta, table_path=table, permutations=99, seed=123)

    assert r1["results"] == r2["results"]
    for key in ("study", "diagnosis"):
        stats = r1["results"][key]
        assert set(stats) == {"column", "groups", "sample_size", "permutations", "pseudo_F", "R2", "p_value"}
        assert stats["permutations"] == 99
        assert 0.0 <= stats["R2"] <= 1.0
        assert 0.0 < stats["p_value"] <= 1.0

    raw = (ws1.metrics_dir / "between.json").read_text(encoding="utf-8").lower()
    assert not re.search(r"\b(good|bad|bom|ruim|significant|significativo|recommend\w*)\b", raw)
    assert ws1.ledger.entries()[-1]["event"] == "between_metrics"
    assert ws1.ledger.verify().ok


def test_cli_between(tmp_path):
    table, meta = _write_between_inputs(tmp_path)
    ws = Workspace.create(tmp_path / "ws", name="t")
    assert main([
        "-w", str(ws.root),
        "between",
        "--metadata", str(meta),
        "--table", str(table),
        "--permutations", "49",
        "--seed", "7",
    ]) == 0
    out = json.loads((ws.metrics_dir / "between.json").read_text(encoding="utf-8"))
    assert out["seed"] == 7 and out["permutations"] == 49
    assert main(["-w", str(ws.root), "verify"]) == 0
