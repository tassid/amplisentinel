"""Stage 4: real-time Nextflow weblog listener on 127.0.0.1."""

import json
import threading
import urllib.error
import urllib.request

import pytest

from sentinel import listen
from sentinel.workspace import Workspace


def _post_json(url: str, payload: dict | str) -> tuple[int, dict]:
    raw = payload.encode("utf-8") if isinstance(payload, str) else json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=raw, method="POST", headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        return exc.code, json.loads(exc.read().decode("utf-8"))


def test_listen_records_weblog_events_in_order_and_preserves_chain(tmp_path):
    ws = Workspace.create(tmp_path / "ws", name="t")
    server = listen.make_server(ws, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        port = server.server_address[1]
        url = f"http://127.0.0.1:{port}/events"
        outdir = str(ws.root / "runs" / "P1" / "results")

        events = [
            {"event": "started", "runName": "p1_run", "utcTime": "2026-11-19T20:00:00Z"},
            {
                "event": "process_started",
                "runName": "p1_run",
                "utcTime": "2026-11-19T20:00:01Z",
                "trace": {"task_id": 1, "hash": "ab/1234", "process": "CUTADAPT_BASIC", "status": "RUNNING"},
                "metadata": {"workflow": {"params": {"outdir": outdir}}},
            },
            {
                "event": "process_completed",
                "runName": "p1_run",
                "utcTime": "2026-11-19T20:00:05Z",
                "trace": {"task_id": 1, "hash": "ab/1234", "process": "CUTADAPT_BASIC", "status": "COMPLETED", "exit": 0},
                "metadata": {"workflow": {"params": {"outdir": outdir}}},
            },
            {
                "event": "process_started",
                "runName": "p1_run",
                "utcTime": "2026-11-19T20:00:06Z",
                "trace": {"task_id": 2, "hash": "cd/5678", "process": "DADA2_DENOISING", "status": "RUNNING"},
                "metadata": {"workflow": {"params": {"outdir": outdir}}},
            },
            {
                "event": "process_completed",
                "runName": "p1_run",
                "utcTime": "2026-11-19T20:00:09Z",
                "trace": {"task_id": 2, "hash": "cd/5678", "process": "DADA2_DENOISING", "status": "FAILED", "exit": 137},
                "metadata": {"workflow": {"params": {"outdir": outdir}}},
            },
        ]
        for ev in events:
            code, body = _post_json(url, ev)
            assert code == 200 and body["ok"] is True

        bad_code, _ = _post_json(url, "{not-json")
        assert bad_code == 400
    finally:
        server.shutdown()
        server.server_close()

    entries = ws.ledger.entries()
    assert [e["event"] for e in entries] == [
        "process_started",
        "process_completed",
        "process_started",
        "process_failed",
    ]
    assert [e["data"]["process"] for e in entries] == [
        "CUTADAPT_BASIC",
        "CUTADAPT_BASIC",
        "DADA2_DENOISING",
        "DADA2_DENOISING",
    ]
    assert all(e["data"]["run"] == "P1" for e in entries)
    assert entries[-1]["data"]["exit_code"] == 137
    assert ws.ledger.verify().ok


def test_listen_refuses_non_loopback_host(tmp_path):
    ws = Workspace.create(tmp_path / "ws", name="t")
    with pytest.raises(ValueError, match="127.0.0.1"):
        listen.make_server(ws, host="0.0.0.0", port=0)
