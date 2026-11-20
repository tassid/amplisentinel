"""Stage 4: local HTTP listener for Nextflow `-with-weblog` events.

`amplisentinel listen` binds strictly to `127.0.0.1` using Python's standard
library and appends `process_started`, `process_completed`, and `process_failed`
entries to the workspace ledger in arrival order.
"""

from __future__ import annotations

import json
import os
import re
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

from .run import DEFAULT_WEBLOG_PORT, settings
from .workspace import Workspace

LOOPBACK = "127.0.0.1"
RUN_PATH_RE = re.compile(r"(?:^|[\\/])runs[\\/]([^\\/]+)(?:[\\/]|$)")


def resolve_run(
    payload: dict[str, Any],
    *,
    query: dict[str, list[str]] | None = None,
    default_run: str | None = None,
) -> str | None:
    """Infer the sequencing run name from query params, Nextflow params, or --run."""
    if query and query.get("run"):
        val = query["run"][0].strip()
        if val:
            return val
    if isinstance(payload.get("run"), str) and payload["run"].strip():
        return payload["run"].strip()
    meta = payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
    wf = meta.get("workflow") if isinstance(meta.get("workflow"), dict) else {}
    params = wf.get("params") if isinstance(wf.get("params"), dict) else {}
    for key in ("outdir", "input"):
        raw = params.get(key)
        if isinstance(raw, str):
            m = RUN_PATH_RE.search(raw)
            if m:
                return m.group(1)
    if default_run:
        return default_run
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    tag = trace.get("tag")
    if isinstance(tag, str) and tag.strip():
        return tag.strip()
    return None


def map_weblog_event(payload: dict[str, Any]) -> str | None:
    """Map one Nextflow weblog payload to a AmpliSentinel ledger event name."""
    nf_event = str(payload.get("event") or "").strip().lower()
    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    status = str(trace.get("status") or "").strip().upper()
    exit_code = trace.get("exit")

    if nf_event == "process_started":
        return "process_started"
    if nf_event == "process_completed":
        failed = status in {"FAILED", "ABORTED"} or (exit_code is not None and exit_code != 0)
        return "process_failed" if failed else "process_completed"
    if nf_event == "error":
        return "process_failed"
    return None


def handle_weblog_payload(
    ws: Workspace,
    payload: dict[str, Any],
    *,
    default_run: str | None = None,
    query: dict[str, list[str]] | None = None,
    lock: threading.Lock | None = None,
) -> dict[str, Any]:
    """Process one parsed Nextflow weblog JSON payload and write to the ledger."""
    ledger_event = map_weblog_event(payload)
    if ledger_event is None:
        return {"ok": True, "recorded": False, "nf_event": payload.get("event")}

    trace = payload.get("trace") if isinstance(payload.get("trace"), dict) else {}
    run_name = resolve_run(payload, query=query, default_run=default_run)
    data = {
        "run": run_name,
        "process": trace.get("process") or trace.get("name"),
        "task_id": trace.get("task_id"),
        "hash": trace.get("hash"),
        "tag": trace.get("tag"),
        "status": trace.get("status"),
        "exit_code": trace.get("exit"),
        "run_name": payload.get("runName"),
        "utc_time": payload.get("utcTime"),
    }
    if lock is not None:
        with lock:
            entry = ws.ledger.append(ledger_event, data)
    else:
        entry = ws.ledger.append(ledger_event, data)
    return {"ok": True, "recorded": True, "event": ledger_event, "hash": entry["hash"], "run": run_name}


class ExclusiveHTTPServer(ThreadingHTTPServer):
    """On Windows SO_REUSEADDR lets a second server bind a port already in use
    and silently miss the requests. Refuse the bind instead."""

    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def make_server(
    ws: Workspace,
    *,
    host: str = LOOPBACK,
    port: int | None = None,
    default_run: str | None = None,
) -> ThreadingHTTPServer:
    """Create the local-only HTTP server for Nextflow `-with-weblog`."""
    if host != LOOPBACK:
        raise ValueError(f"amplisentinel listen aceita apenas {LOOPBACK}, não {host}")
    resolved_port = settings(ws).weblog_port if port is None else port
    if isinstance(resolved_port, bool) or not isinstance(resolved_port, int) or not (0 <= resolved_port <= 65535):
        raise ValueError(f"porta inválida: {resolved_port}")

    lock = threading.Lock()

    class Handler(BaseHTTPRequestHandler):
        def _send_json(self, code: int, body: dict[str, Any]) -> None:
            raw = json.dumps(body, ensure_ascii=False).encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path == "/health":
                self._send_json(200, {"ok": True, "workspace": ws.name})
                return
            self._send_json(404, {"ok": False, "error": "not found"})

        def do_POST(self) -> None:
            parsed = urllib.parse.urlparse(self.path)
            if parsed.path != "/events":
                self._send_json(404, {"ok": False, "error": "not found"})
                return
            length = int(self.headers.get("Content-Length", "0") or "0")
            raw = self.rfile.read(length)
            try:
                payload = json.loads(raw.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                self._send_json(400, {"ok": False, "error": f"json inválido: {exc}"})
                return
            if not isinstance(payload, dict):
                self._send_json(400, {"ok": False, "error": "esperado objeto JSON"})
                return
            query = urllib.parse.parse_qs(parsed.query)
            res = handle_weblog_payload(ws, payload, default_run=default_run, query=query, lock=lock)
            self._send_json(200, res)

        def log_message(self, format: str, *args: Any) -> None:
            return

    return ExclusiveHTTPServer((host, resolved_port), Handler)


def serve(ws: Workspace, *, port: int | None = None, default_run: str | None = None) -> int:
    """Start `amplisentinel listen` until interrupted."""
    server = make_server(ws, port=port if port is not None else settings(ws).weblog_port or DEFAULT_WEBLOG_PORT,
                         default_run=default_run)
    host, actual_port = server.server_address[:2]
    print(f"escutando weblog em http://{host}:{actual_port}/events (workspace: {ws.name})", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0
