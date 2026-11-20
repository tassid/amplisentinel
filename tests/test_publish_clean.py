"""Stage 6: publish and clean with fakes for rclone and DVC."""

import hashlib
import json
from pathlib import Path

import pytest

from amplisentinel import publish
from amplisentinel.cli import main
from amplisentinel.ext import CallResult
from amplisentinel.workspace import Workspace


def _md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


class FakePublishTools:
    """Simulates rclone copyto/lsjson and dvc add/push."""

    def __init__(self, *, corrupt_remote: str | None = None):
        self.remote: dict[str, bytes] = {}
        self.corrupt_remote = corrupt_remote
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CallResult:
        self.calls.append(list(argv))
        if argv[:2] == ["rclone", "version"]:
            return CallResult(0, b"rclone v1.68.0-test\n", b"")
        if argv[:2] == ["rclone", "copyto"]:
            src = Path(argv[2])
            dest = argv[3].split(":", 1)[1].split("/", 2)[-1]
            self.remote[dest] = src.read_bytes()
            return CallResult(0, b"", b"")
        if argv[:2] == ["rclone", "lsjson"]:
            items = []
            for rel, data in sorted(self.remote.items()):
                digest = "0" * 32 if rel == self.corrupt_remote else _md5(data)
                items.append(
                    {
                        "Name": rel.rsplit("/", 1)[-1],
                        "Path": rel,
                        "IsDir": False,
                        "Hashes": {"md5": digest},
                    }
                )
            return CallResult(0, json.dumps(items).encode("utf-8"), b"")
        if argv[0] == "dvc":
            if argv[1] == "version":
                return CallResult(0, b"3.55.0\n", b"")
            if argv[1] == "add":
                target = Path(argv[2])
                target.with_suffix(target.suffix + ".dvc").write_text("outs: []\n", encoding="utf-8")
            return CallResult(0, b"", b"")
        raise AssertionError(f"unexpected call: {argv}")


def _measured_ws(tmp_path: Path) -> Workspace:
    ws = Workspace.create(tmp_path / "ws", name="cervical")
    ws.runs_file.write_text("runs:\n  P1:\n    region: V4\n", encoding="utf-8")
    ws.state.register(["P1"])
    ws.state.move("P1", "FETCHING")
    ws.state.move("P1", "MD5_OK")
    ws.state.move("P1", "PROCESSING")
    ws.state.move("P1", "MEASURED")

    run_dir = ws.root / "runs" / "P1"
    (run_dir / "fastq").mkdir(parents=True)
    (run_dir / "fastq" / "SRR1_1.fastq.gz").write_bytes(b"fq-data-1")
    (run_dir / "work" / "tmp").mkdir(parents=True)
    (run_dir / "work" / "tmp" / "intermediate.bin").write_bytes(b"work-bytes")
    (run_dir / "results" / "qiime2").mkdir(parents=True)
    (run_dir / "results" / "qiime2" / "table.qza").write_bytes(b"qza-table")
    (run_dir / "results" / "qiime2" / "rep.qzv").write_bytes(b"qzv-view")
    ws.metrics_dir.mkdir(parents=True, exist_ok=True)
    (ws.metrics_dir / "P1.json").write_text(
        json.dumps({"run": "P1", "values": {"dada2.filtered_pct": {"value": 90.0}}}),
        encoding="utf-8",
    )
    return ws


def test_clean_refused_in_every_state_except_published(tmp_path):
    ws = _measured_ws(tmp_path)
    tools = FakePublishTools()
    with pytest.raises(publish.CleanRefused, match="MEASURED"):
        publish.clean_run(ws, "P1", runner=tools)
    assert (ws.root / "runs" / "P1" / "fastq" / "SRR1_1.fastq.gz").exists()
    assert main(["-w", str(ws.root), "clean", "P1"]) == 2


def test_publish_and_clean_verify_remote_hashes_and_record_mlflow(tmp_path):
    ws = _measured_ws(tmp_path)
    tools = FakePublishTools()
    pub = publish.publish_run(ws, "P1", runner=tools)
    assert pub["state"] == "PUBLISHED"
    assert ws.state.get("P1") == "PUBLISHED"

    # MLflow run records git commit and ledger hash
    if pub["mlflow"] is not None:
        import mlflow

        mlflow.set_tracking_uri(pub["mlflow"]["tracking_uri"])
        run_obj = mlflow.get_run(pub["mlflow"]["run_id"])
        assert "git_commit" in run_obj.data.tags
        assert "ledger_head" in run_obj.data.tags

    # Divergent remote hash blocks clean without deleting any file
    tools.corrupt_remote = "artifacts/results/qiime2/table.qza"
    with pytest.raises(publish.CleanRefused, match="hash remoto divergente"):
        publish.clean_run(ws, "P1", runner=tools)
    assert ws.state.get("P1") == "PUBLISHED"
    assert (ws.root / "runs" / "P1" / "fastq" / "SRR1_1.fastq.gz").exists()
    assert (ws.root / "runs" / "P1" / "work" / "tmp" / "intermediate.bin").exists()

    # Valid remote hashes allow clean -> CLEANED
    tools.corrupt_remote = None
    cln = publish.clean_run(ws, "P1", runner=tools)
    assert cln["state"] == "CLEANED"
    assert ws.state.get("P1") == "CLEANED"
    assert not (ws.root / "runs" / "P1" / "fastq").exists()
    assert not (ws.root / "runs" / "P1" / "work").exists()
    cleaned_ev = [e for e in ws.ledger.entries() if e["event"] == "cleaned"][-1]
    assert cleaned_ev["data"]["deleted_count"] == 2
    assert ws.ledger.verify().ok
