"""Stage 7: project merge, SILVA MD5 verification, and replay-supplement."""

import hashlib
from pathlib import Path

import pytest

from sentinel import merge
from sentinel.cli import main
from sentinel.ext import CallResult
from sentinel.workspace import Workspace

QIIME_IMAGE = "quay.io/qiime2/amplicon:2024.10@sha256:" + "e" * 64


class FakeMergeTools:
    def __init__(self):
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CallResult:
        self.calls.append(list(argv))
        if argv[1] in ("version", "--version"):
            return CallResult(0, b"docker version 27.3.1\n", b"")
        for flag in ("--o-merged-table", "--o-merged-data", "--o-classification", "--o-out-fp"):
            if flag in argv:
                p = Path(argv[argv.index(flag) + 1])
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(f"artifact:{p.name}\n".encode())
        return CallResult(0, b"ok\n", b"")


def _setup_merge_ws(tmp_path: Path, *, regions: tuple[str, str] = ("V4", "V4"), bad_md5: bool = False) -> Workspace:
    ws = Workspace.create(tmp_path / "ws", name="cervical")
    silva = ws.root / "silva-138.qza"
    silva.write_bytes(b"silva-classifier-bytes")
    silva_md5 = "0" * 32 if bad_md5 else hashlib.md5(silva.read_bytes()).hexdigest()

    (ws.root / "sentinel.yaml").write_text(
        f"name: cervical\ntitle: cervical\nmerge:\n  qiime2_image: '{QIIME_IMAGE}'\n  silva_classifier: silva-138.qza\n  silva_md5: '{silva_md5}'\n",
        encoding="utf-8",
    )
    ws.runs_file.write_text(
        f"runs:\n  P1:\n    region: {regions[0]}\n  P2:\n    region: {regions[1]}\n",
        encoding="utf-8",
    )
    ws.state.register(["P1", "P2"])
    for r in ("P1", "P2"):
        ws.state.move(r, "FETCHING")
        ws.state.move(r, "MD5_OK")
        ws.state.move(r, "PROCESSING")
        ws.state.move(r, "MEASURED")
        ws.state.move(r, "PUBLISHED")
        q2_in = ws.root / "runs" / r / "results" / "qiime2" / "input"
        q2_in.mkdir(parents=True, exist_ok=True)
        (q2_in / "table.qza").write_bytes(f"table-{r}".encode())
        (q2_in / "rep-seqs.qza").write_bytes(f"seqs-{r}".encode())
    return ws


def test_different_regions_block_until_human_decide_merge(tmp_path):
    ws = _setup_merge_ws(tmp_path, regions=("V4", "V3-V4"))
    tools = FakeMergeTools()
    blocked = merge.run_merge(ws, runner=tools)
    assert blocked["status"] == "BLOCKED"
    assert blocked["regions_by_run"] == {"P1": "V4", "P2": "V3-V4"}
    assert tools.calls == []

    # Researcher records strategy via `sentinel decide merge "..."`
    strategy = "trim to common V4 region before merging"
    assert main(["-w", str(ws.root), "decide", "merge", strategy]) == 0

    done = merge.run_merge(ws, runner=tools)
    assert done["status"] == "COMPLETED"
    assert done["strategy"] == strategy
    assert "replay_supplement" in done["artifacts"]
    ev = [e for e in ws.ledger.entries() if e["event"] == "merge"][-1]
    assert ev["data"]["strategy"] == strategy
    assert ev["data"]["artifacts"]["replay_supplement"]["sha256"]
    assert ws.ledger.verify().ok


def test_merge_requires_all_runs_published_or_cleaned_and_valid_silva_md5(tmp_path):
    ws = _setup_merge_ws(tmp_path)
    ws.runs_file.write_text("runs:\n  P1:\n    region: V4\n  P2:\n    region: V4\n  P3:\n    region: V4\n", encoding="utf-8")
    ws.state.register(["P3"])
    with pytest.raises(merge.MergeRefused, match="PUBLISHED ou CLEANED"):
        merge.run_merge(ws, runner=FakeMergeTools())

    ws_bad = _setup_merge_ws(tmp_path / "bad", bad_md5=True)
    res = merge.run_merge(ws_bad, runner=FakeMergeTools())
    assert res["status"] == "BLOCKED"
    assert "MD5 do SILVA" in res["reason"]
