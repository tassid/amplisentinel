"""Stage 1: validated runs.yaml, Drive reconciliation with fakes (no network, no rclone)."""

import csv
import json

import pytest

from amplisentinel import reconcile
from amplisentinel.ext import CallResult, ToolError
from amplisentinel.runs import ConfigError, parse_runs
from amplisentinel.workspace import Workspace

ENA_DB = {
    "SRR1": {
        "run_accession": "SRR1",
        "study_accession": "PRJNA1",
        "library_layout": "PAIRED",
        "instrument_model": "Illumina MiSeq",
        "read_count": "100",
        "fastq_ftp": "ftp.sra.ebi.ac.uk/vol1/SRR1_1.fastq.gz;ftp.sra.ebi.ac.uk/vol1/SRR1_2.fastq.gz",
        "fastq_md5": "aaaa1111;bbbb2222",
        "fastq_bytes": "10;20",
    },
    "SRR2": {
        "run_accession": "SRR2",
        "study_accession": "PRJNA1",
        "library_layout": "PAIRED",
        "instrument_model": "Illumina MiSeq",
        "read_count": "50",
        "fastq_ftp": "ftp.sra.ebi.ac.uk/vol1/SRR2_1.fastq.gz",
        "fastq_md5": "cccc3333",
        "fastq_bytes": "5",
    },
}


def fake_ena(batch):
    return [ENA_DB[a] for a in batch if a in ENA_DB]


class FakeRclone:
    """Answers `rclone version` and `rclone lsjson`. Records every argv it is given."""

    def __init__(self, listing=None, returncode=0, stderr=b""):
        self.listing = listing if listing is not None else []
        self.returncode = returncode
        self.stderr = stderr
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[1] == "version":
            return CallResult(0, b"rclone v9.9.9-test\n- os/arch: test\n", b"")
        if self.returncode != 0:
            return CallResult(self.returncode, b"", self.stderr)
        return CallResult(0, json.dumps(self.listing).encode("utf-8"), b"")


def make_workspace(tmp_path, runs_yaml: str) -> Workspace:
    ws = Workspace.create(tmp_path / "ws", name="teste")
    ws.runs_file.write_text(runs_yaml, encoding="utf-8")
    (ws.accessions_dir / "S1_runs.txt").write_text("SRR1\nSRR2\n", encoding="utf-8")
    return ws


RUNS_WITH_DRIVE = """runs:
  S1:
    accessions: data/accessions/S1_runs.txt
    region: null
    primers: { forward: null, reverse: null }
    drive_path: Pasta-S1
"""

RUNS_WITHOUT_DRIVE = """runs:
  S1:
    accessions: data/accessions/S1_runs.txt
    region: null
    primers: { forward: null, reverse: null }
"""


def read_tsv(path):
    with path.open(encoding="utf-8") as fh:
        return list(csv.DictReader(fh, delimiter="\t"))


# --- runs.yaml schema -------------------------------------------------------

def test_valid_runs_parse_and_null_region_is_allowed():
    runs = parse_runs(
        {
            "runs": {
                "P10a": {
                    "accessions": "data/accessions/P10_runs.txt",
                    "region": None,
                    "primers": {"forward": "GTGCCAGCMGCCGCGGTAA", "reverse": None},
                    "drive_path": "P10a",
                }
            }
        }
    )
    cfg = runs["P10a"]
    assert cfg.region is None
    assert cfg.primers.forward == "GTGCCAGCMGCCGCGGTAA"
    assert cfg.drive_path == "P10a"
    assert cfg.metadata is None


def test_unknown_run_key_is_refused_and_lists_accepted_keys():
    data = {"runs": {"S1": {"accessions": "a.txt", "region": None,
                            "primers": {"forward": None, "reverse": None}, "truncation": 240}}}
    with pytest.raises(ConfigError) as exc:
        parse_runs(data)
    msg = str(exc.value)
    assert "truncation" in msg
    assert "accessions, region, primers, drive_path, metadata" in msg


def test_unknown_top_level_and_primer_keys_are_refused():
    with pytest.raises(ConfigError, match="chave desconhecida"):
        parse_runs({"runs": {}, "extra": 1})
    with pytest.raises(ConfigError, match="primers: chave desconhecida"):
        parse_runs({"runs": {"S1": {"accessions": "a.txt", "region": None,
                                    "primers": {"forward": None, "reverse": None, "x": 1}}}})


def test_missing_keys_mean_not_filled_yet():
    cfg = parse_runs({"runs": {"S1": {}}})["S1"]
    assert cfg.accessions is None and cfg.region is None
    assert cfg.primers.forward is None and cfg.primers.reverse is None


def test_wrong_type_is_refused():
    with pytest.raises(ConfigError, match="region"):
        parse_runs({"runs": {"S1": {"region": 16}}})


def test_empty_file_means_no_runs(tmp_path):
    ws = Workspace.create(tmp_path / "ws", name="vazio")
    assert ws.configured_runs() == []


# --- reconciliation ENA x Drive ---------------------------------------------

def test_reconciliation_flags_md5_mismatch_and_missing_file(tmp_path):
    ws = make_workspace(tmp_path, RUNS_WITH_DRIVE)
    rclone = FakeRclone(
        listing=[
            {"Path": "SRR1_1.fastq.gz", "Name": "SRR1_1.fastq.gz", "IsDir": False,
             "Hashes": {"md5": "AAAA1111"}},
            {"Path": "SRR1_2.fastq.gz", "Name": "SRR1_2.fastq.gz", "IsDir": False,
             "Hashes": {"md5": "dddd9999"}},
            {"Path": "sub", "Name": "sub", "IsDir": True},
        ]
    )
    summary = reconcile.build(ws, fetcher=fake_ena, runner=rclone)

    rows = read_tsv(ws.inventory_dir / "reconciliation.tsv")
    by_file = {r["expected_file"]: r for r in rows}
    assert by_file["SRR1_1.fastq.gz"]["equal"] == "yes"          # case-insensitive md5
    assert by_file["SRR1_2.fastq.gz"]["present_in_drive"] == "yes"
    assert by_file["SRR1_2.fastq.gz"]["equal"] == "no"            # md5 differs from ENA
    assert by_file["SRR2_1.fastq.gz"]["present_in_drive"] == "no"
    assert by_file["SRR2_1.fastq.gz"]["equal"] == "no"
    assert summary["files"] == 3
    assert summary["present_in_drive"] == 2
    assert summary["equal"] == 1


def test_reconciliation_only_lists_drive_never_downloads(tmp_path):
    ws = make_workspace(tmp_path, RUNS_WITH_DRIVE)
    rclone = FakeRclone(listing=[])
    reconcile.build(ws, fetcher=fake_ena, runner=rclone)
    lists = [c for c in rclone.calls if c[1] != "version"]
    assert lists == [["rclone", "lsjson", "--hash", "--recursive", "gdrive_ro:Pasta-S1"]]


def test_reconciliation_ledger_entry_has_file_hash_and_chain_is_intact(tmp_path):
    ws = make_workspace(tmp_path, RUNS_WITH_DRIVE)
    reconcile.build(ws, fetcher=fake_ena, runner=FakeRclone(listing=[]))
    entries = ws.ledger.entries()
    last = entries[-1]
    assert last["event"] == "reconciliation_built"
    out = ws.inventory_dir / "reconciliation.tsv"
    import hashlib

    assert last["data"]["output"]["sha256"] == hashlib.sha256(out.read_bytes()).hexdigest()
    tool_calls = [e for e in entries if e["event"] == "tool_call"]
    assert tool_calls and tool_calls[-1]["data"]["version"] == "rclone v9.9.9-test"
    assert tool_calls[-1]["data"]["exit_code"] == 0
    assert ws.ledger.verify().ok


def test_run_without_drive_path_is_not_checked(tmp_path):
    ws = make_workspace(tmp_path, RUNS_WITHOUT_DRIVE)
    rclone = FakeRclone(listing=[])
    summary = reconcile.build(ws, fetcher=fake_ena, runner=rclone)
    rows = read_tsv(ws.inventory_dir / "reconciliation.tsv")
    assert {r["present_in_drive"] for r in rows} == {"not_checked"}
    assert {r["equal"] for r in rows} == {"not_checked"}
    assert summary["runs_without_drive_path"] == ["S1"]
    assert all(c[1] == "version" for c in rclone.calls)


def test_duplicate_name_on_drive_is_not_equal(tmp_path):
    ws = make_workspace(tmp_path, RUNS_WITH_DRIVE)
    rclone = FakeRclone(
        listing=[
            {"Name": "SRR2_1.fastq.gz", "IsDir": False, "Hashes": {"md5": "cccc3333"}},
            {"Name": "SRR2_1.fastq.gz", "IsDir": False, "Hashes": {"md5": "cccc3333"}},
        ]
    )
    reconcile.build(ws, fetcher=fake_ena, runner=rclone)
    row = {r["expected_file"]: r for r in read_tsv(ws.inventory_dir / "reconciliation.tsv")}["SRR2_1.fastq.gz"]
    assert row["present_in_drive"] == "yes" and row["equal"] == "no"


def test_accession_not_in_ena_gives_row_without_match(tmp_path):
    ws = Workspace.create(tmp_path / "ws", name="t")
    ws.runs_file.write_text(RUNS_WITHOUT_DRIVE, encoding="utf-8")
    (ws.accessions_dir / "S1_runs.txt").write_text("SRR999\n", encoding="utf-8")
    reconcile.build(ws, fetcher=fake_ena, runner=FakeRclone())
    row = read_tsv(ws.inventory_dir / "reconciliation.tsv")[0]
    assert row["ena_found"] == "no" and row["accession"] == "SRR999"


def test_rclone_failure_keeps_literal_stderr_and_ledger_error(tmp_path):
    ws = make_workspace(tmp_path, RUNS_WITH_DRIVE)
    stderr = b"Failed to create file system for \"gdrive_ro:Pasta-S1\": didn't find section\n"
    rclone = FakeRclone(returncode=1, stderr=stderr)
    with pytest.raises(ToolError, match="saiu com código 1"):
        reconcile.build(ws, fetcher=fake_ena, runner=rclone)

    logs = list((ws.home / "logs").glob("*-rclone.stderr"))
    assert len(logs) == 1 and logs[0].read_bytes() == stderr
    events = [e["event"] for e in ws.ledger.entries()]
    assert "error" in events and events[-1] == "error"
    assert not (ws.inventory_dir / "reconciliation.tsv").exists()


def test_missing_accession_file_is_reported(tmp_path):
    ws = Workspace.create(tmp_path / "ws", name="t")
    ws.runs_file.write_text(RUNS_WITHOUT_DRIVE, encoding="utf-8")
    from amplisentinel.workspace import WorkspaceError

    with pytest.raises(WorkspaceError, match="arquivo de accessions não encontrado"):
        reconcile.build(ws, fetcher=fake_ena, runner=FakeRclone())


# --- projects.tsv counts -----------------------------------------------------

def test_projects_tsv_counts_studies_and_layouts(tmp_path):
    from amplisentinel import inventory as inv

    acc = tmp_path / "acc"
    acc.mkdir()
    (acc / "S1_runs.txt").write_text("SRR1\nSRR2\n", encoding="utf-8")
    inv.build(acc, tmp_path / "out", fetcher=fake_ena)
    row = read_tsv(tmp_path / "out" / "projects.tsv")[0]
    assert row["studies"] == "PRJNA1"
    assert row["n_studies"] == "1"
    assert row["n_library_layouts"] == "1"
    assert row["n_instrument_models"] == "1"
