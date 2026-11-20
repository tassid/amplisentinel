"""Stage 2: fetch worker with fakes for ENA, rclone, Nextflow and HTTP (no network)."""

import hashlib
import http.server
import json
import threading
from pathlib import Path

import pytest

from amplisentinel import fetch
from amplisentinel.cli import main
from amplisentinel.ext import CallResult
from amplisentinel.runs import ConfigError
from amplisentinel.state import TransitionError
from amplisentinel.workspace import Workspace

CONTENT = {
    "SRR1_1.fastq.gz": b"@r1\nACGT\n+\nIIII\n" * 50,
    "SRR1_2.fastq.gz": b"@r1\nTGCA\n+\nIIII\n" * 50,
}


def md5(b: bytes) -> str:
    return hashlib.md5(b).hexdigest()


def ena_record(md5_override=None):
    names = list(CONTENT)
    md5s = [md5(CONTENT[n]) for n in names]
    if md5_override:
        md5s = [md5_override.get(n, m) for n, m in zip(names, md5s)]
    return {
        "run_accession": "SRR1",
        "fastq_ftp": ";".join(f"ftp.sra.ebi.ac.uk/vol1/fastq/SRR1/{n}" for n in names),
        "fastq_md5": ";".join(md5s),
        "fastq_bytes": ";".join(str(len(CONTENT[n])) for n in names),
    }


def fake_ena(record):
    return lambda batch: [record] if "SRR1" in batch else []


class FakeDownloader:
    """Serves CONTENT. Can cut the first download of each file in half."""

    def __init__(self, interrupt=False):
        self.interrupt = interrupt
        self.calls: list[tuple[str, int]] = []
        self.cut: set[str] = set()

    def __call__(self, url: str, dest: Path) -> None:
        name = url.rsplit("/", 1)[-1]
        offset = dest.stat().st_size if dest.exists() else 0
        self.calls.append((name, offset))
        data = CONTENT[name][offset:]
        if self.interrupt and name not in self.cut:
            self.cut.add(name)
            with dest.open("ab") as fh:
                fh.write(data[: len(data) // 2])
            raise ConnectionResetError("conexão encerrada pelo servidor")
        with dest.open("ab") as fh:
            fh.write(data)


class FakeTools:
    """rclone and nextflow. Records argv."""

    def __init__(self, listing=None, list_rc=0, produce=None):
        self.listing = listing or []
        self.list_rc = list_rc
        self.produce = produce or {}
        self.calls: list[list[str]] = []

    def __call__(self, argv):
        self.calls.append(list(argv))
        if argv[1] in ("version", "-version"):
            return CallResult(0, f"{argv[0]} 9.9.9-test\n".encode(), b"")
        if argv[:2] == ["rclone", "lsjson"]:
            if self.list_rc:
                return CallResult(self.list_rc, b"", b"couldn't list directory\n")
            return CallResult(0, json.dumps(self.listing).encode(), b"")
        if argv[:2] == ["rclone", "copyto"]:
            name = argv[2].rsplit("/", 1)[-1]
            Path(argv[3]).write_bytes(CONTENT[name])
            return CallResult(0, b"", b"")
        if argv[0] == "nextflow":
            outdir = Path(argv[argv.index("--outdir") + 1]) / "fastq"
            outdir.mkdir(parents=True, exist_ok=True)
            for name, data in self.produce.items():
                (outdir / name).write_bytes(data)
            return CallResult(0, b"", b"")
        raise AssertionError(argv)


def make_ws(tmp_path, drive_path=None, meta_extra="") -> Workspace:
    ws = Workspace.create(tmp_path / "ws", name="t")
    drive = f"\n    drive_path: {drive_path}" if drive_path else ""
    ws.runs_file.write_text(
        "runs:\n  S1:\n    accessions: data/accessions/S1_runs.txt\n"
        f"    region: null\n    primers: {{forward: null, reverse: null}}{drive}\n",
        encoding="utf-8",
    )
    (ws.accessions_dir / "S1_runs.txt").write_text("SRR1\n", encoding="utf-8")
    if meta_extra:
        with (ws.root / "amplisentinel.yaml").open("a", encoding="utf-8") as fh:
            fh.write(meta_extra)
    ws.state.register(ws.configured_runs())
    return ws


def BIG(path):
    return 10**12


def test_fetch_from_ena_reaches_md5_ok_with_manifest(tmp_path):
    ws = make_ws(tmp_path)
    dl = FakeDownloader()
    res = fetch.run_fetch(ws, "S1", fetcher=fake_ena(ena_record()), runner=FakeTools(),
                          downloader=dl, free_bytes=BIG)
    assert res["state"] == "MD5_OK" and ws.state.get("S1") == "MD5_OK"
    manifest_path = Path(res["manifest"]["path"])
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert {f["source"] for f in manifest["files"]} == {"ena"}
    assert all(f["equal"] for f in manifest["files"])
    done = [e for e in ws.ledger.entries() if e["event"] == "fetch_completed"][-1]
    assert done["data"]["manifest"]["sha256"] == hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    assert len([e for e in ws.ledger.entries() if e["event"] == "file_verified"]) == 2
    assert ws.ledger.verify().ok


def test_interrupted_download_blocks_then_resumes_from_partial(tmp_path):
    ws = make_ws(tmp_path)
    dl = FakeDownloader(interrupt=True)
    first = fetch.run_fetch(ws, "S1", fetcher=fake_ena(ena_record()), runner=FakeTools(),
                            downloader=dl, free_bytes=BIG)
    assert first["state"] == "BLOCKED"
    parts = sorted(p.name for p in (ws.root / "runs" / "S1" / "fastq").glob("*.part"))
    assert parts == ["SRR1_1.fastq.gz.part", "SRR1_2.fastq.gz.part"]

    with pytest.raises(fetch.FetchRefused, match="BLOCKED"):
        fetch.run_fetch(ws, "S1", fetcher=fake_ena(ena_record()), runner=FakeTools(),
                        downloader=dl, free_bytes=BIG)
    with pytest.raises(TransitionError):
        ws.state.move("S1", "FETCHING")
    ws.state.move("S1", "FETCHING", justification="conexão caiu; retomar")

    second = fetch.run_fetch(ws, "S1", fetcher=fake_ena(ena_record()), runner=FakeTools(),
                             downloader=dl, free_bytes=BIG)
    assert second["state"] == "MD5_OK"
    resumed = [(n, off) for n, off in dl.calls if off > 0]
    assert {n for n, _ in resumed} == set(CONTENT)
    assert ws.ledger.verify().ok


def test_wrong_md5_blocks_and_keeps_the_file(tmp_path):
    ws = make_ws(tmp_path)
    rec = ena_record({"SRR1_2.fastq.gz": "0" * 32})
    res = fetch.run_fetch(ws, "S1", fetcher=fake_ena(rec), runner=FakeTools(),
                          downloader=FakeDownloader(), free_bytes=BIG)
    assert res["state"] == "BLOCKED"
    err = [e for e in ws.ledger.entries() if e["event"] == "error"][-1]
    assert [p["file"] for p in err["data"]["files"]] == ["SRR1_2.fastq.gz"]
    bad = ws.root / "runs" / "S1" / "fastq" / "SRR1_2.fastq.gz"
    assert bad.read_bytes() == CONTENT["SRR1_2.fastq.gz"]

    ws.state.move("S1", "FETCHING", justification="conferir de novo")
    dl = FakeDownloader()
    again = fetch.run_fetch(ws, "S1", fetcher=fake_ena(rec), runner=FakeTools(),
                            downloader=dl, free_bytes=BIG)
    assert again["state"] == "BLOCKED"
    assert dl.calls == []  # present files are checked as they are, never overwritten
    assert bad.exists()


def test_drive_is_used_when_file_is_there(tmp_path):
    ws = make_ws(tmp_path, drive_path="S1")
    tools = FakeTools(listing=[{"Name": "SRR1_1.fastq.gz", "Path": "raw/SRR1_1.fastq.gz", "IsDir": False,
                                "Hashes": {"md5": md5(CONTENT["SRR1_1.fastq.gz"])}}])
    dl = FakeDownloader()
    res = fetch.run_fetch(ws, "S1", fetcher=fake_ena(ena_record()), runner=tools,
                          downloader=dl, free_bytes=BIG)
    assert res["state"] == "MD5_OK"
    copies = [c for c in tools.calls if c[:2] == ["rclone", "copyto"]]
    assert copies and copies[0][2] == "gdrive_ro:S1/raw/SRR1_1.fastq.gz"
    assert [n for n, _ in dl.calls] == ["SRR1_2.fastq.gz"]  # absent on Drive -> ENA
    manifest = json.loads(Path(res["manifest"]["path"]).read_text(encoding="utf-8"))
    assert {f["file"]: f["source"] for f in manifest["files"]} == {
        "SRR1_1.fastq.gz": "drive", "SRR1_2.fastq.gz": "ena"}


def test_drive_listing_failure_blocks_without_downloading(tmp_path):
    ws = make_ws(tmp_path, drive_path="S1")
    dl = FakeDownloader()
    res = fetch.run_fetch(ws, "S1", fetcher=fake_ena(ena_record()), runner=FakeTools(list_rc=1),
                          downloader=dl, free_bytes=BIG)
    assert res["state"] == "BLOCKED" and dl.calls == []
    assert any(p.get("log") for p in res["problems"])


def test_not_enough_disk_refuses_without_state_change(tmp_path):
    ws = make_ws(tmp_path, meta_extra="fetch:\n  disk_slack: 2\n")
    total = sum(len(v) for v in CONTENT.values())
    with pytest.raises(fetch.FetchRefused, match="espaço livre"):
        fetch.run_fetch(ws, "S1", fetcher=fake_ena(ena_record()), runner=FakeTools(),
                        downloader=FakeDownloader(), free_bytes=lambda p: total * 2 - 1)
    assert ws.state.get("S1") == "PENDING"
    err = ws.ledger.entries()[-1]
    assert err["event"] == "error" and err["data"]["disk_required"] == total * 2


def test_unknown_fetch_setting_is_refused(tmp_path):
    ws = make_ws(tmp_path, meta_extra="fetch:\n  threads: 4\n")
    with pytest.raises(ConfigError, match="threads"):
        fetch.settings(ws)


def test_fetchngs_requires_pinned_release(tmp_path):
    ws = make_ws(tmp_path)
    with pytest.raises(fetch.FetchRefused, match="fetchngs_release"):
        fetch.run_fetch(ws, "S1", via="fetchngs", fetcher=fake_ena(ena_record()), runner=FakeTools(),
                        free_bytes=BIG)
    assert ws.state.get("S1") == "PENDING"


def test_fetchngs_files_are_checked_against_ena(tmp_path):
    ws = make_ws(tmp_path, meta_extra="fetch:\n  fetchngs_release: 9.9.9\n")
    produce = {f"SRX1_{n}": data for n, data in CONTENT.items()}
    tools = FakeTools(produce=produce)
    res = fetch.run_fetch(ws, "S1", via="fetchngs", fetcher=fake_ena(ena_record()), runner=tools,
                          free_bytes=BIG)
    assert res["state"] == "MD5_OK"
    nf = next(c for c in tools.calls if c[0] == "nextflow" and "run" in c)
    assert nf[nf.index("-r") + 1] == "9.9.9" and nf[nf.index("-profile") + 1] == "docker"
    tool_call = [e for e in ws.ledger.entries() if e["event"] == "tool_call"][-1]
    assert tool_call["data"]["command"] == nf and tool_call["data"]["run"] == "S1"


def test_cli_fetch_exit_codes(tmp_path, monkeypatch):
    ws = make_ws(tmp_path)
    assert main(["-w", str(ws.root), "fetch", "NOPE"]) == 2
    monkeypatch.setattr(fetch.inv, "ena_fetch", fake_ena(ena_record()))
    monkeypatch.setattr(fetch, "http_download", FakeDownloader())
    monkeypatch.setattr(fetch, "disk_free", BIG)
    # defaults are bound at definition time, so pass through the module attributes
    original = fetch.run_fetch

    def patched(ws_, run, **kw):
        return original(ws_, run, fetcher=fetch.inv.ena_fetch, downloader=fetch.http_download,
                        free_bytes=fetch.disk_free, **kw)

    monkeypatch.setattr(fetch, "run_fetch", patched)
    assert main(["-w", str(ws.root), "fetch", "S1"]) == 0
    assert main(["-w", str(ws.root), "verify"]) == 0


# --- real HTTP download with Range, on a loopback server ------------------------

class RangeHandler(http.server.BaseHTTPRequestHandler):
    data = b"0123456789" * 1000

    def do_GET(self):
        rng = self.headers.get("Range")
        start = int(rng.split("=")[1].rstrip("-")) if rng else 0
        body = self.data[start:]
        self.send_response(206 if rng else 200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def test_http_download_resumes_with_range(tmp_path):
    server = http.server.HTTPServer(("127.0.0.1", 0), RangeHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        dest = tmp_path / "f.part"
        dest.write_bytes(RangeHandler.data[:3333])
        fetch.http_download(f"http://127.0.0.1:{server.server_port}/f", dest)
        assert dest.read_bytes() == RangeHandler.data
    finally:
        server.shutdown()
