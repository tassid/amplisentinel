import json

import pytest

from amplisentinel import inventory as inv
from amplisentinel.ledger import Ledger
from amplisentinel.state import RunState, TransitionError


def test_ledger_chain_and_tamper(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    led.append("a", {"x": 1})
    led.append("b", {"x": 2})
    led.append("c", {"x": 3})
    assert led.verify().ok

    lines = led.path.read_text().splitlines()
    entry = json.loads(lines[1])
    entry["data"]["x"] = 999
    lines[1] = json.dumps(entry)
    led.path.write_text("\n".join(lines) + "\n")
    res = led.verify()
    assert not res.ok and res.broken_at == 2


def test_ledger_deleted_line_detected(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    for i in range(3):
        led.append("e", {"i": i})
    lines = led.path.read_text().splitlines()
    led.path.write_text(lines[0] + "\n" + lines[2] + "\n")
    assert not led.verify().ok


def test_state_transitions(tmp_path):
    led = Ledger(tmp_path / "ledger.jsonl")
    st = RunState(tmp_path / "state.json", led)
    st.register(["P1"])
    st.move("P1", "FETCHING")
    with pytest.raises(TransitionError):
        st.move("P1", "PUBLISHED")
    st.move("P1", "BLOCKED")
    with pytest.raises(TransitionError):
        st.move("P1", "FETCHING")
    st.move("P1", "FETCHING", justification="MD5 conferido manualmente")
    last = led.entries()[-1]
    assert last["actor"] == "human"
    assert led.verify().ok


def test_inventory_with_fake_ena(tmp_path):
    acc = tmp_path / "acc"
    acc.mkdir()
    (acc / "P1_runs.txt").write_text("SRR1  \nSRR2\n\njunk\n")
    (acc / "P2_runs.txt").write_text("ERR9\n")

    def fake(batch):
        db = {
            "SRR1": {"run_accession": "SRR1", "library_layout": "PAIRED", "instrument_model": "Illumina MiSeq",
                     "study_accession": "PRJNA1", "read_count": "100", "fastq_ftp": "a;b", "fastq_bytes": "10;20"},
            "SRR2": {"run_accession": "SRR2", "library_layout": "PAIRED", "instrument_model": "Illumina MiSeq",
                     "study_accession": "PRJNA1", "read_count": "50", "fastq_ftp": "c;d", "fastq_bytes": "5;5"},
        }
        return [db[a] for a in batch if a in db]

    summary = inv.build(acc, tmp_path / "out", fetcher=fake)
    assert summary["accessions"] == 3
    assert summary["not_found"] == 1
    rows = (tmp_path / "out" / "projects.tsv").read_text().splitlines()
    p1 = rows[1].split("\t")
    assert p1[0] == "P1" and p1[1] == "2" and p1[8] == "150" and p1[9] == "4"
