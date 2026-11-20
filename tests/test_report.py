"""Stage 11: deterministic static HTML report."""

import json
import re

from amplisentinel import report
from amplisentinel.cli import main
from amplisentinel.workspace import Workspace


def _strip_timestamp(html_text: str) -> str:
    text = re.sub(r'<meta name="generated-at" content="[^"]+">', "", html_text)
    return re.sub(r"Generated at: <code>[^<]+</code>", "", text)


def test_report_is_reproducible_and_copies_human_decisions_and_qzv_verbatim(tmp_path):
    ws = Workspace.create(tmp_path / "ws", name="cervical", title="Cervical cancer")
    ws.runs_file.write_text("runs:\n  P1: {}\n  P14: {}\n", encoding="utf-8")
    ws.state.register(["P1", "P14"])
    ws.state.move("P1", "FETCHING")
    ws.state.move("P1", "BLOCKED")
    ws.state.move("P1", "FETCHING", justification="refetch after timeout")
    literal_decision = "keep V4 region and separate V3V4 group"
    ws.ledger.append("decision", {"run": "merge", "text": literal_decision}, actor="human")

    ws.metrics_dir.mkdir(parents=True, exist_ok=True)
    (ws.metrics_dir / "P1.json").write_text(
        json.dumps(
            {
                "run": "P1",
                "values": {
                    "dada2.merged_pct": {
                        "value": 82.5,
                        "unit": "%",
                        "rule": "dada2.merged_pct_min",
                        "result": "within",
                        "source": {"path": "results/dada2/DADA2_stats.tsv", "sha256": "a" * 64},
                    }
                },
            }
        ),
        encoding="utf-8",
    )
    qzv = ws.root / "runs" / "P1" / "results" / "qiime2" / "barplot.qzv"
    qzv.parent.mkdir(parents=True, exist_ok=True)
    qzv.write_bytes(b"qzv-zip-content")

    r1 = report.generate_report(ws, out_path=tmp_path / "r1.html", generated_at="2026-11-19T00:00:00Z")
    report.generate_report(ws, out_path=tmp_path / "r2.html", generated_at="2026-11-20T00:00:00Z")

    html1 = (tmp_path / "r1.html").read_text(encoding="utf-8")
    html2 = (tmp_path / "r2.html").read_text(encoding="utf-8")
    assert _strip_timestamp(html1) == _strip_timestamp(html2)
    assert r1["qzv_count"] == 1
    assert r1["decisions"] == 2
    assert literal_decision in html1
    assert "refetch after timeout" in html1
    assert "barplot.qzv" in html1 and "view.qiime2.org" in html1
    assert "82.5 %" in html1
    assert "\u2014" not in html1 and "\u2013" not in html1

    assert main(["-w", str(ws.root), "report"]) == 0
    assert (ws.home / "report.html").exists()
