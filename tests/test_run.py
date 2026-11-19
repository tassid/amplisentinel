"""Stage 3: run worker (nf-core/ampliseq 3.1.0) with fakes for Nextflow, Docker, Colima and Conda."""

import hashlib
import json
from pathlib import Path

import pytest
import yaml

from sentinel import run
from sentinel.cli import main
from sentinel.ext import CallResult
from sentinel.runs import ConfigError
from sentinel.state import TransitionError
from sentinel.workspace import Workspace

FAKE_CONDA_YAML = b"name: sentinel\nchannels:\n  - conda-forge\ndependencies:\n  - python=3.11\n"
IMG_CUTADAPT = "quay.io/biocontainers/cutadapt:4.9--py39hbcbf7aa_2"
IMG_DADA2 = "quay.io/biocontainers/bioconductor-dada2:1.30.0--r43hf17093f_0"
DIG_CUTADAPT = "sha256:" + "a" * 64
DIG_DADA2 = "sha256:" + "b" * 64


class FakeRunTools:
    """Fake nextflow, docker/podman, colima, and conda. Records every argv."""

    def __init__(
        self,
        *,
        nf_rc: int = 0,
        nf_stderr: bytes = b"",
        colima_present: bool = True,
        conda_present: bool = True,
        rosetta: bool = True,
        vm_type: str = "vz",
        engine: str = "docker",
    ):
        self.nf_rc = nf_rc
        self.nf_stderr = nf_stderr
        self.colima_present = colima_present
        self.conda_present = conda_present
        self.rosetta = rosetta
        self.vm_type = vm_type
        self.engine = engine
        self.calls: list[list[str]] = []

    def __call__(self, argv: list[str]) -> CallResult:
        self.calls.append(list(argv))
        tool = argv[0]
        if tool == "nextflow":
            if argv[1] == "-version":
                return CallResult(0, b"N E X T F L O W version 26.10.0\n", b"")
            if "inspect" in argv:
                payload = {
                    "processes": [
                        {"name": "CUTADAPT", "container": IMG_CUTADAPT},
                        {"name": "DADA2_DENOISING", "container": IMG_DADA2},
                        {"name": "DADA2_RMCHIMERA", "container": IMG_DADA2},
                    ]
                }
                return CallResult(0, json.dumps(payload).encode(), b"")
            if "run" in argv:
                if self.nf_rc == 0:
                    for flag in ("-with-trace", "-with-report", "-with-timeline", "-log"):
                        if flag in argv:
                            p = Path(argv[argv.index(flag) + 1])
                            p.parent.mkdir(parents=True, exist_ok=True)
                            p.write_text(f"fake {flag}\n", encoding="utf-8")
                return CallResult(self.nf_rc, b"nextflow stdout\n", self.nf_stderr)

        if tool == self.engine:
            if argv[1] in ("version", "--version"):
                return CallResult(0, f"{self.engine} version 27.3.1\n".encode(), b"")
            if argv[1] == "images":
                lines = [
                    f"{IMG_CUTADAPT.rsplit(':', 1)[0]}\t{IMG_CUTADAPT.rsplit(':', 1)[1]}\t{DIG_CUTADAPT}\tid1",
                    f"{IMG_DADA2.rsplit(':', 1)[0]}\t{IMG_DADA2.rsplit(':', 1)[1]}\t{DIG_DADA2}\tid2",
                ]
                return CallResult(0, ("\n".join(lines) + "\n").encode(), b"")

        if tool == "colima":
            if not self.colima_present:
                return CallResult(127, b"", b"colima: command not found\n")
            if argv[1] == "version":
                return CallResult(0, b"colima version 0.8.1\n", b"")
            if argv[1] == "list":
                data = [
                    {
                        "name": "default",
                        "status": "Running",
                        "vmType": self.vm_type,
                        "config": {"vmOpts": {"vz": {"rosetta": {"enabled": self.rosetta, "binfmt": True}}}},
                    }
                ]
                return CallResult(0, json.dumps(data).encode(), b"")

        if tool == "conda":
            if not self.conda_present:
                return CallResult(127, b"", b"conda: command not found\n")
            if argv[1] == "--version":
                return CallResult(0, b"conda 24.9.2\n", b"")
            if argv[1:4] == ["env", "export", "--no-builds"]:
                return CallResult(0, FAKE_CONDA_YAML, b"")

        raise AssertionError(f"unexpected tool call: {argv}")


def make_ready_ws(
    tmp_path: Path,
    *,
    region: str | None = "V3-V4",
    fwd: str | None = "CCTACGGGNGGCWGCAG",
    rev: str | None = "GACTACHVGGGTATCTAATCC",
    paired: bool = True,
    metadata: str | None = None,
    state: str = "MD5_OK",
    meta_extra: str = "",
) -> Workspace:
    ws = Workspace.create(tmp_path / "ws", name="t")
    reg_val = f'"{region}"' if region else "null"
    fwd_val = f'"{fwd}"' if fwd else "null"
    rev_val = f'"{rev}"' if rev else "null"
    meta_line = f'\n    metadata: "{metadata}"' if metadata else ""
    ws.runs_file.write_text(
        "runs:\n"
        "  S1:\n"
        "    accessions: data/accessions/S1_runs.txt\n"
        f"    region: {reg_val}\n"
        f"    primers: {{forward: {fwd_val}, reverse: {rev_val}}}{meta_line}\n",
        encoding="utf-8",
    )
    (ws.accessions_dir / "S1_runs.txt").write_text("SRR1\nSRR2\n", encoding="utf-8")
    if meta_extra:
        with (ws.root / "sentinel.yaml").open("a", encoding="utf-8") as fh:
            fh.write(meta_extra)

    fastq_dir = ws.root / "runs" / "S1" / "fastq"
    fastq_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for acc in ("SRR1", "SRR2"):
        names = [f"{acc}_1.fastq.gz", f"{acc}_2.fastq.gz"] if paired else [f"{acc}.fastq.gz"]
        for name in names:
            (fastq_dir / name).write_bytes(b"@r\nACGT\n+\nIIII\n")
            files.append({"accession": acc, "file": name, "equal": True})
    (ws.root / "runs" / "S1" / "fetch_manifest.json").write_text(
        json.dumps({"run": "S1", "files": files}),
        encoding="utf-8",
    )
    ws.state.register(["S1"])
    if state != "PENDING":
        ws.state.move("S1", "FETCHING")
    if state in ("MD5_OK", "PROCESSING", "BLOCKED"):
        if state == "BLOCKED":
            ws.state.move("S1", "BLOCKED")
        else:
            ws.state.move("S1", "MD5_OK")
        if state == "PROCESSING":
            ws.state.move("S1", "PROCESSING")
    return ws


def test_samplesheet_and_params_match_ampliseq_3_1_0_fixture(tmp_path):
    ws = make_ready_ws(tmp_path, metadata="config/meta.tsv")
    (ws.root / "config" / "meta.tsv").write_text("ID\tgroup\nSRR1\ta\nSRR2\tb\n", encoding="utf-8")
    tools = FakeRunTools()
    res = run.run_ampliseq(ws, "S1", runner=tools, environ={"CONDA_PREFIX": "/envs/sentinel"})

    assert res["state"] == "PROCESSING"
    assert ws.state.get("S1") == "PROCESSING"

    ss_path = ws.root / "runs" / "S1" / "ampliseq" / "samplesheet.tsv"
    fq_dir = (ws.root / "runs" / "S1" / "fastq").resolve()
    expected_ss = (
        "sample\tfastq_1\tfastq_2\trun\n"
        f"SRR1\t{fq_dir / 'SRR1_1.fastq.gz'}\t{fq_dir / 'SRR1_2.fastq.gz'}\tS1\n"
        f"SRR2\t{fq_dir / 'SRR2_1.fastq.gz'}\t{fq_dir / 'SRR2_2.fastq.gz'}\tS1\n"
    )
    assert ss_path.read_text(encoding="utf-8") == expected_ss

    params_path = ws.root / "runs" / "S1" / "ampliseq" / "params.yaml"
    params = yaml.safe_load(params_path.read_text(encoding="utf-8"))
    assert params == {
        "input": str(ss_path),
        "outdir": str(ws.root / "runs" / "S1" / "results"),
        "primer_fwd": "CCTACGGGNGGCWGCAG",
        "primer_rev": "GACTACHVGGGTATCTAATCC",
        "metadata": str((ws.root / "config" / "meta.tsv").resolve()),
    }
    assert "trunclenf" not in params and "trunclenr" not in params


def test_single_end_samplesheet_omits_fastq_2_and_sets_sequencing_type(tmp_path):
    ws = make_ready_ws(tmp_path, paired=False)
    tools = FakeRunTools()
    res = run.run_ampliseq(ws, "S1", runner=tools, environ={})
    assert res["state"] == "PROCESSING"

    ss_lines = (ws.root / "runs" / "S1" / "ampliseq" / "samplesheet.tsv").read_text(encoding="utf-8").splitlines()
    assert ss_lines[0] == "sample\tfastq_1\trun"
    assert len(ss_lines) == 3

    params = yaml.safe_load((ws.root / "runs" / "S1" / "ampliseq" / "params.yaml").read_text(encoding="utf-8"))
    assert params["sequencing_type"] == "illumina_se"


def test_exact_nextflow_command_and_environment_logged_before_run(tmp_path):
    ws = make_ready_ws(tmp_path, meta_extra="run:\n  ampliseq_release: '3.1.0'\n  weblog_port: 8999\n")
    tools = FakeRunTools()
    run.run_ampliseq(ws, "S1", runner=tools, environ={"CONDA_PREFIX": "/opt/conda/envs/sentinel"})

    entries = ws.ledger.entries()
    events = [e["event"] for e in entries]
    env_idx = events.index("environment")
    nf_call_idx = max(
        i for i, e in enumerate(entries)
        if e["event"] == "tool_call" and e["data"]["command"][:2] == ["nextflow", "-log"]
    )
    assert env_idx < nf_call_idx

    env = entries[env_idx]["data"]
    assert env["ampliseq_release"] == "3.1.0"
    assert env["nextflow_version"] == "N E X T F L O W version 26.10.0"
    assert env["colima_version"] == "colima version 0.8.1"
    assert env["docker_version"] == "docker version 27.3.1"
    assert env["vm_config"]["vmType"] == "vz"
    assert env["vm_config"]["rosetta"] is True
    assert env["images"] == [
        {"image": IMG_CUTADAPT, "digest": DIG_CUTADAPT},
        {"image": IMG_DADA2, "digest": DIG_DADA2},
    ]
    assert env["conda_env"]["available"] is True
    assert env["conda_env"]["sha256"] == hashlib.sha256(FAKE_CONDA_YAML).hexdigest()

    nf_cmd = entries[nf_call_idx]["data"]["command"]
    assert nf_cmd[0] == "nextflow"
    assert nf_cmd[nf_cmd.index("run") + 1] == "nf-core/ampliseq"
    assert nf_cmd[nf_cmd.index("-r") + 1] == "3.1.0"
    assert nf_cmd[nf_cmd.index("-profile") + 1] == "docker"
    assert "-resume" in nf_cmd
    assert "-with-trace" in nf_cmd
    assert "-with-report" in nf_cmd
    assert "-with-timeline" in nf_cmd
    assert nf_cmd[nf_cmd.index("-with-weblog") + 1] == "http://127.0.0.1:8999/events"
    assert ws.ledger.verify().ok


def test_podman_engine_uses_podman_profile(tmp_path):
    ws = make_ready_ws(tmp_path, meta_extra="container_engine: podman\n")
    tools = FakeRunTools(engine="podman", colima_present=False, conda_present=False)
    res = run.run_ampliseq(ws, "S1", runner=tools, environ={})
    assert res["state"] == "PROCESSING"
    nf_cmd = next(c for c in tools.calls if c[:2] == ["nextflow", "-log"])
    assert nf_cmd[nf_cmd.index("-profile") + 1] == "podman"
    env = [e["data"] for e in ws.ledger.entries() if e["event"] == "environment"][-1]
    assert env["colima_version"] is None and env["vm_config"] is None
    assert env["conda_env"]["available"] is False


def test_nextflow_failure_moves_to_blocked_with_literal_stderr(tmp_path):
    ws = make_ready_ws(tmp_path)
    literal_err = b"ERROR ~ Error executing process > 'NFCORE_AMPLISEQ:AMPLISEQ:DADA2_DENOISING'\nExit status 137\n"
    tools = FakeRunTools(nf_rc=1, nf_stderr=literal_err)
    res = run.run_ampliseq(ws, "S1", runner=tools, environ={})

    assert res["state"] == "BLOCKED"
    assert ws.state.get("S1") == "BLOCKED"
    err = [e for e in ws.ledger.entries() if e["event"] == "error"][-1]
    stderr_path = Path(err["data"]["stderr"]["path"])
    assert stderr_path.read_bytes() == literal_err
    assert err["data"]["stderr"]["sha256"] == hashlib.sha256(literal_err).hexdigest()

    with pytest.raises(run.RunRefused, match="BLOCKED"):
        run.run_ampliseq(ws, "S1", runner=tools, environ={})
    with pytest.raises(TransitionError):
        ws.state.move("S1", "PROCESSING")
    ws.state.move("S1", "PROCESSING", justification="ajustado limite de memória da VM; retomar")
    resumed = run.run_ampliseq(ws, "S1", runner=FakeRunTools(), environ={})
    assert resumed["state"] == "PROCESSING"
    assert ws.state.get("S1") == "PROCESSING"
    assert ws.ledger.verify().ok


def test_refuses_without_state_change_when_scientific_fields_or_manifest_missing(tmp_path):
    ws_no_region = make_ready_ws(tmp_path / "r", region=None)
    with pytest.raises(run.RunRefused, match="region"):
        run.run_ampliseq(ws_no_region, "S1", runner=FakeRunTools())
    assert ws_no_region.state.get("S1") == "MD5_OK"

    ws_no_primers = make_ready_ws(tmp_path / "p", fwd=None, rev=None)
    with pytest.raises(run.RunRefused, match="primers"):
        run.run_ampliseq(ws_no_primers, "S1", runner=FakeRunTools())
    assert ws_no_primers.state.get("S1") == "MD5_OK"

    ws_no_meta = make_ready_ws(tmp_path / "meta", metadata="config/missing.tsv")
    with pytest.raises(run.RunRefused, match="metadata"):
        run.run_ampliseq(ws_no_meta, "S1", runner=FakeRunTools())
    assert ws_no_meta.state.get("S1") == "MD5_OK"

    ws_pending = make_ready_ws(tmp_path / "s", state="PENDING")
    with pytest.raises(run.RunRefused, match="PENDING"):
        run.run_ampliseq(ws_pending, "S1", runner=FakeRunTools())
    assert ws_pending.state.get("S1") == "PENDING"

    ws_no_manifest = make_ready_ws(tmp_path / "m")
    (ws_no_manifest.root / "runs" / "S1" / "fetch_manifest.json").unlink()
    with pytest.raises(run.RunRefused, match="fetch_manifest.json"):
        run.run_ampliseq(ws_no_manifest, "S1", runner=FakeRunTools())
    assert ws_no_manifest.state.get("S1") == "MD5_OK"


def test_unknown_run_setting_is_refused(tmp_path):
    ws = make_ready_ws(tmp_path, meta_extra="run:\n  trunclenf: 240\n")
    with pytest.raises(ConfigError, match="trunclenf"):
        run.settings(ws)


def test_cli_run_exit_codes(tmp_path, monkeypatch):
    ws = make_ready_ws(tmp_path)
    assert main(["-w", str(ws.root), "run", "NOPE"]) == 2

    tools = FakeRunTools()
    original = run.run_ampliseq

    def patched(ws_, run_name, **kw):
        return original(ws_, run_name, runner=tools, environ={}, **kw)

    monkeypatch.setattr(run, "run_ampliseq", patched)
    assert main(["-w", str(ws.root), "run", "S1"]) == 0
    assert main(["-w", str(ws.root), "verify"]) == 0
