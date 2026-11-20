"""Browser interface (`amplisentinel ui`) and cross-process ledger lock."""

import http.client
import subprocess
import sys
import threading
from urllib.parse import urlencode

import pytest

from amplisentinel import ui
from amplisentinel.ledger import Ledger
from amplisentinel.workspace import Workspace


def _ws(tmp_path):
    ws = Workspace.create(tmp_path / "ws", name="cervical", title="Cervical cancer")
    ws.runs_file.write_text("runs:\n  P1: {}\n  P14: {}\n", encoding="utf-8")
    ws.state.register(["P1", "P14"])
    return ws


def test_ledger_chain_survives_parallel_processes(tmp_path):
    path = tmp_path / "ledger.jsonl"
    code = (
        "import sys; from amplisentinel.ledger import Ledger\n"
        "l = Ledger(sys.argv[1])\n"
        "for i in range(25): l.append('t', {'i': i, 'p': sys.argv[2]})\n"
    )
    procs = [subprocess.Popen([sys.executable, "-c", code, str(path), str(n)]) for n in range(4)]
    assert all(p.wait(timeout=120) == 0 for p in procs)
    res = Ledger(path).verify()
    assert res.ok, res
    assert res.entries == 100


def test_build_args_refusals(tmp_path):
    ws = _ws(tmp_path)
    assert ui.build_args(ws, "verify", {}) == ["verify"]
    assert ui.build_args(ws, "fetch", {"run": "P1", "source": "ena"}) == ["fetch", "P1", "--source", "ena"]
    assert ui.build_args(ws, "run-deblur", {"run": "P1"}) == ["run", "P1", "--denoiser", "deblur"]
    with pytest.raises(ui.UiRefused):
        ui.build_args(ws, "fetch", {"run": "--help"})
    with pytest.raises(ui.UiRefused):
        ui.build_args(ws, "fetch", {"run": "P1", "source": "ftp"})
    with pytest.raises(ui.UiRefused):
        ui.build_args(ws, "clean", {"run": "P1"})
    assert ui.build_args(ws, "clean", {"run": "P1", "confirm": "yes"}) == ["clean", "P1"]
    with pytest.raises(ui.UiRefused):
        ui.build_args(ws, "shell", {"run": "P1"})
    with pytest.raises(ui.UiRefused):
        ui.build_args(ws, "move", {"run": "P1", "to": "CLEANED"})
    with pytest.raises(ui.UiRefused):
        ui.build_args(ws, "decide", {"run": "P1", "text": "  "})
    ws.state.move("P1", "FETCHING")
    ws.state.move("P1", "BLOCKED")
    with pytest.raises(ui.UiRefused):
        ui.build_args(ws, "move", {"run": "P1", "to": "PENDING", "justification": ""})
    assert ui.build_args(ws, "move", {"run": "P1", "to": "PENDING", "justification": "texto"}) == [
        "move", "P1", "PENDING", "-j", "texto",
    ]


@pytest.fixture
def server(tmp_path):
    ws = _ws(tmp_path)
    srv = ui.UiServer(ws, 0)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def _req(srv, method, path, body=None, host=None, headers=None):
    conn = http.client.HTTPConnection("127.0.0.1", srv.port, timeout=10)
    h = {"Host": host or f"127.0.0.1:{srv.port}"}
    if body is not None:
        h["Content-Type"] = "application/x-www-form-urlencoded"
    h.update(headers or {})
    conn.request(method, path, body=body, headers=h)
    resp = conn.getresponse()
    data = resp.read().decode("utf-8")
    conn.close()
    return resp.status, resp.getheader("Location"), data


def test_pages_render(server):
    server.ws.ledger.append("process_completed", {"run": "P1", "process": "DADA2_DENOISING", "task_id": 7})
    status, _, body = _req(server, "GET", "/")
    assert status == 200 and "P14" in body and "PENDING" in body
    status, _, body = _req(server, "GET", "/run/P1")
    assert status == 200 and "DADA2_DENOISING" in body
    assert _req(server, "GET", "/run/P99")[0] == 404
    assert _req(server, "GET", "/ledger")[0] == 200
    assert _req(server, "GET", "/report")[0] == 200
    assert _req(server, "GET", "/jobs")[0] == 200


def test_host_token_and_origin_checks(server):
    assert _req(server, "GET", "/", host="evil.example:80")[0] == 403
    body = urlencode({"token": "wrong"})
    assert _req(server, "POST", "/action/verify", body)[0] == 403
    body = urlencode({"token": server.token})
    assert _req(server, "POST", "/action/verify", body, headers={"Origin": "http://evil.example"})[0] == 403
    assert _req(server, "POST", "/action/fetch", urlencode({"token": server.token, "run": "P99"}))[0] == 409
    assert not [e for e in server.ws.ledger.entries() if e["event"].startswith("ui_command")]


def test_action_runs_cli_and_is_recorded(server):
    body = urlencode({"token": server.token})
    status, location, _ = _req(
        server, "POST", "/action/verify", body, headers={"Origin": f"http://127.0.0.1:{server.port}"}
    )
    assert status == 303 and location.startswith("/job/")
    server.jobs.wait_all()
    job = server.jobs.get(location[len("/job/"):])
    assert job.exit_code == 0
    status, _, page = _req(server, "GET", location)
    assert status == 200 and "ledger íntegro" in page
    events = [e for e in server.ws.ledger.entries() if e["event"].startswith("ui_command")]
    assert [e["event"] for e in events] == ["ui_command", "ui_command_finished"]
    assert events[0]["actor"] == "human"
    assert events[1]["data"]["exit_code"] == 0 and events[1]["data"]["log"]["sha256"]
    assert server.ws.ledger.verify().ok


def test_one_command_at_a_time(tmp_path):
    ws = _ws(tmp_path)
    gate = threading.Event()

    class Slow:
        def wait(self):
            gate.wait(10)
            return 0

    jobs = ui.JobManager(ws, launcher=lambda argv, log: Slow())
    jobs.start(["verify"])
    with pytest.raises(ui.UiRefused):
        jobs.start(["verify"])
    gate.set()
    jobs.wait_all()
    assert jobs.start(["verify"]).status == "running"
    gate.set()
    jobs.wait_all()


def test_port_in_use_is_refused(tmp_path):
    ws = _ws(tmp_path)
    first = ui.UiServer(ws, 0)
    try:
        with pytest.raises(OSError):
            ui.UiServer(ws, first.port)
    finally:
        first.server_close()


def test_mascot_in_header_and_favicon(server):
    status, _, body = _req(server, "GET", "/")
    assert status == 200
    assert body.count("data:image/svg+xml;base64,") == 2
    assert ui.MASCOT.startswith("data:image/svg+xml;base64,")
