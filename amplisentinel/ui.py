"""Local browser interface for one workspace.

`amplisentinel ui` serves pages on 127.0.0.1 only. Pages show the same data as
`amplisentinel show` and the MCP server (values, states, paths, sha256). Buttons run
the same `amplisentinel` commands as the terminal, one at a time, in a separate
process, so every refusal and every ledger event is identical to the CLI.

Safety:
- binds to 127.0.0.1 only and rejects any Host header other than
  127.0.0.1:<port> or localhost:<port> (DNS rebinding);
- every POST needs a random token created when the server starts, and a
  matching Origin when the browser sends one (cross site requests);
- run names must exist in the workspace, so no value reaches the command line
  as an option;
- each command is recorded in the ledger (`ui_command`, `ui_command_finished`)
  with the literal argv, the exit code and the sha256 of its output log.

Nothing here interprets results or recommends a next step.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import secrets
import subprocess
import sys
import threading
import webbrowser
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlparse

from . import __version__, listen, report
from . import queries as q
from .common import utc_iso
from .state import TRANSITIONS
from .workspace import Workspace

DEFAULT_PORT = 8800
HOST = "127.0.0.1"

# Workspace level buttons: label, argv after `amplisentinel -w <ws>`.
WS_ACTIONS: dict[str, tuple[str, list[str]]] = {
    "init": ("init", ["init"]),
    "inventory": ("inventory", ["inventory"]),
    "inventory-drive": ("inventory --drive", ["inventory", "--drive"]),
    "verify": ("verify", ["verify"]),
    "report": ("report", ["report"]),
    "merge": ("merge", ["merge"]),
}

# Run level buttons: label, argv with {run} replaced by the run name.
RUN_ACTIONS: dict[str, tuple[str, list[str]]] = {
    "fetch": ("fetch", ["fetch", "{run}"]),
    "run": ("run (DADA2, ampliseq)", ["run", "{run}"]),
    "run-deblur": ("run --denoiser deblur", ["run", "{run}", "--denoiser", "deblur"]),
    "measure": ("measure", ["measure", "{run}"]),
    "decontam": ("decontam", ["decontam", "{run}"]),
    "publish": ("publish", ["publish", "{run}"]),
    "clean": ("clean", ["clean", "{run}"]),
}



def _mascot_data_uri() -> str:
    path = Path(__file__).with_name("assets") / "mascote.svg"
    if not path.exists():
        return ""
    return "data:image/svg+xml;base64," + base64.b64encode(path.read_bytes()).decode("ascii")


MASCOT = _mascot_data_uri()

FETCH_SOURCES = ("auto", "drive", "ena")


class UiRefused(Exception):
    pass


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _json(value: Any) -> str:
    return _esc(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))


def _source(src: dict | None) -> str:
    if not src:
        return ""
    if not src.get("exists", True):
        return f'<p class="src">fonte: <code>{_esc(src.get("path"))}</code> (não existe)</p>'
    sha = src.get("sha256") or ""
    return f'<p class="src">fonte: <code>{_esc(src.get("path"))}</code> sha256=<code>{_esc(sha[:16])}</code></p>'


# --------------------------------------------------------------------- jobs


@dataclass
class Job:
    id: str
    argv: list[str]
    log_path: Path
    started: str
    status: str = "running"  # running, finished
    exit_code: int | None = None
    finished: str | None = None
    log_sha256: str | None = None
    proc: Any = field(default=None, repr=False)


def default_launcher(argv: list[str], log_path: Path) -> subprocess.Popen:
    env = dict(os.environ, PYTHONIOENCODING="utf-8", PYTHONUTF8="1")
    fh = log_path.open("wb")
    try:
        return subprocess.Popen(
            [sys.executable, "-m", "amplisentinel.cli", *argv],
            stdout=fh,
            stderr=subprocess.STDOUT,
            stdin=subprocess.DEVNULL,
            env=env,
        )
    finally:
        fh.close()


class JobManager:
    """Runs one `amplisentinel` command at a time for one workspace."""

    def __init__(self, ws: Workspace, launcher: Callable[[list[str], Path], Any] = default_launcher):
        self.ws = ws
        self.launcher = launcher
        self.jobs: list[Job] = []
        self.lock = threading.Lock()
        self.dir = ws.home / "ui" / "jobs"

    def running(self) -> Job | None:
        with self.lock:
            return next((j for j in self.jobs if j.status == "running"), None)

    def get(self, job_id: str) -> Job | None:
        with self.lock:
            return next((j for j in self.jobs if j.id == job_id), None)

    def start(self, args: list[str]) -> Job:
        with self.lock:
            busy = next((j for j in self.jobs if j.status == "running"), None)
            if busy is not None:
                raise UiRefused(f"já existe um comando em execução: {busy.id} ({' '.join(busy.argv)})")
            self.dir.mkdir(parents=True, exist_ok=True)
            job_id = f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}-{len(self.jobs) + 1:03d}"
            argv = ["-w", str(self.ws.root), *args]
            log_path = self.dir / f"{job_id}.log"
            job = Job(id=job_id, argv=argv, log_path=log_path, started=utc_iso())
            self.ws.ledger.append(
                "ui_command",
                {"job": job_id, "argv": ["amplisentinel", *argv], "log": str(log_path), "run": _run_of(args)},
                actor="human",
            )
            job.proc = self.launcher(argv, log_path)
            self.jobs.append(job)
        threading.Thread(target=self._wait, args=(job,), daemon=True).start()
        return job

    def _wait(self, job: Job) -> None:
        code = job.proc.wait()
        sha = hashlib.sha256(job.log_path.read_bytes()).hexdigest() if job.log_path.exists() else None
        with self.lock:
            self.ws.ledger.append(
                "ui_command_finished",
                {
                    "job": job.id,
                    "argv": ["amplisentinel", *job.argv],
                    "exit_code": code,
                    "log": {"path": str(job.log_path), "sha256": sha},
                    "run": _run_of(job.argv[2:]),
                },
            )
            job.exit_code = code
            job.log_sha256 = sha
            job.finished = utc_iso()
            job.status = "finished"

    def wait_all(self, timeout: float = 30.0) -> None:
        """Used by tests: block until no job is running."""
        import time

        end = time.monotonic() + timeout
        while self.running() is not None and time.monotonic() < end:
            time.sleep(0.05)


def _run_of(args: list[str]) -> str | None:
    if args and args[0] in {"fetch", "run", "measure", "decontam", "publish", "clean", "move", "decide"}:
        return args[1] if len(args) > 1 else None
    return None


# ---------------------------------------------------------------- commands


def build_args(ws: Workspace, action: str, form: dict[str, str]) -> list[str]:
    """Translate one form into argv. Raises UiRefused for anything not allowed."""
    known = set(ws.state.all()) | set(ws.configured_runs())
    if action in WS_ACTIONS:
        return list(WS_ACTIONS[action][1])
    run = form.get("run", "")
    if run not in known:
        raise UiRefused(f"corrida desconhecida neste workspace: {run}")
    if action in RUN_ACTIONS:
        args = [a.replace("{run}", run) for a in RUN_ACTIONS[action][1]]
        if action == "fetch":
            src = form.get("source", "auto")
            if src not in FETCH_SOURCES:
                raise UiRefused(f"origem inválida: {src}")
            args += ["--source", src]
        if action == "clean" and form.get("confirm") != "yes":
            raise UiRefused("clean exige marcar a confirmação")
        return args
    if action == "move":
        to = form.get("to", "")
        current = ws.state.get(run)
        if to not in TRANSITIONS.get(current, set()):
            raise UiRefused(f"transição não permitida: {current} -> {to}")
        text = form.get("justification", "").strip()
        if current == "BLOCKED" and not text:
            raise UiRefused("sair de BLOCKED exige justificativa escrita pela pesquisadora")
        args = ["move", run, to]
        if text:
            args += ["-j", text]
        return args
    if action == "decide":
        text = form.get("text", "").strip()
        if not text:
            raise UiRefused("a decisão precisa de texto")
        return ["decide", run, text]
    raise UiRefused(f"ação desconhecida: {action}")


# -------------------------------------------------------------------- pages

CSS = """
body{font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;margin:0;color:#1d2433;background:#f6f7f9}
header{background:#1d2433;color:#fff;padding:12px 24px;display:flex;gap:24px;align-items:center}
header a{color:#cfd8ea;text-decoration:none}header b{font-size:18px}header img{border-radius:50%;margin-right:-12px}
main{padding:20px 24px;max-width:1200px}
table{border-collapse:collapse;width:100%;background:#fff}
th,td{border:1px solid #dde1e8;padding:6px 8px;text-align:left;font-size:14px;vertical-align:top}
th{background:#eef1f5}
pre{background:#fff;border:1px solid #dde1e8;padding:10px;overflow:auto;font-size:12px;max-height:420px}
code{font-size:12px}.src{color:#5a6475;font-size:12px;margin:4px 0 16px}
form.inline{display:inline-block;margin:0 6px 6px 0}
button{background:#2f5bd3;color:#fff;border:0;border-radius:4px;padding:6px 12px;cursor:pointer}
button.warn{background:#a33}
.state{font-family:monospace;padding:2px 6px;border-radius:3px;background:#eef1f5}
.BLOCKED{background:#f6d5d5}.MD5_OK,.MEASURED,.PUBLISHED,.CLEANED{background:#d9efdf}
.msg{background:#fff4d6;border:1px solid #e6c97a;padding:8px 12px;margin-bottom:16px}
fieldset{background:#fff;border:1px solid #dde1e8;margin-bottom:12px}
textarea{width:100%;min-height:60px}
"""


def _page(ws: Workspace, title: str, body: str, refresh: bool = False) -> str:
    meta = '<meta http-equiv="refresh" content="3">' if refresh else ""
    icon = f'<link rel="icon" type="image/svg+xml" href="{MASCOT}">' if MASCOT else ""
    logo = f'<img src="{MASCOT}" alt="" width="36" height="36">' if MASCOT else ""
    return (
        "<!doctype html><html lang=\"pt-BR\"><head><meta charset=\"utf-8\">"
        f"{meta}{icon}<title>{_esc(title)} | AmpliSentinel {_esc(ws.name)}</title><style>{CSS}</style></head><body>"
        f"<header>{logo}<b>AmpliSentinel</b><span>{_esc(ws.name)}</span>"
        '<a href="/">corridas</a><a href="/ledger">ledger</a><a href="/jobs">comandos</a>'
        '<a href="/report">relatório</a>'
        f'<span style="margin-left:auto;font-size:12px">v{_esc(__version__)} 127.0.0.1</span></header>'
        f"<main><h2>{_esc(title)}</h2>{body}</main></body></html>"
    )


def _form(token: str, action: str, label: str, hidden: dict[str, str] | None = None,
          extra: str = "", cls: str = "") -> str:
    fields = "".join(
        f'<input type="hidden" name="{_esc(k)}" value="{_esc(v)}">' for k, v in (hidden or {}).items()
    )
    klass = f' class="{cls}"' if cls else ""
    return (
        f'<form class="inline" method="post" action="/action/{_esc(action)}">'
        f'<input type="hidden" name="token" value="{_esc(token)}">{fields}{extra}'
        f"<button{klass}>{_esc(label)}</button></form>"
    )


def _running_note(jobs: JobManager) -> str:
    j = jobs.running()
    if j is None:
        return ""
    return (
        f'<p class="msg">em execução: <a href="/job/{_esc(j.id)}">{_esc(j.id)}</a> '
        f"<code>amplisentinel {_esc(' '.join(j.argv[2:]))}</code></p>"
    )


def page_home(ws: Workspace, token: str, jobs: JobManager, msg: str = "") -> str:
    info = q.workspace_info(ws)
    st = q.status(ws)
    states = st["runs"]
    runs = sorted(set(states) | set(info["runs_configured"]))
    rows = "".join(
        f'<tr><td><a href="/run/{_esc(r)}">{_esc(r)}</a></td>'
        f'<td><span class="state {_esc(states.get(r, ""))}">{_esc(states.get(r, "não registrada"))}</span></td></tr>'
        for r in runs
    )
    counts = ", ".join(f"{_esc(k)}={_esc(v)}" for k, v in sorted(st["counts_by_state"].items())) or "nenhuma"
    actions = "".join(_form(token, k, lbl) for k, (lbl, _) in WS_ACTIONS.items())
    body = (
        (f'<p class="msg">{_esc(msg)}</p>' if msg else "")
        + _running_note(jobs)
        + f"<p>{_esc(info.get('title'))}</p><p>{_esc(info.get('description'))}</p>"
        + f"<fieldset><legend>comandos do workspace</legend>{actions}</fieldset>"
        + f"<p>corridas por estado: {counts}</p>"
        + f"<table><tr><th>corrida</th><th>estado</th></tr>{rows}</table>"
        + _source(st["source"])
    )
    return _page(ws, "Corridas", body, refresh=jobs.running() is not None)


def page_run(ws: Workspace, token: str, jobs: JobManager, run: str) -> str:
    detail = q.run_detail(ws, run)
    metrics = q.run_metrics(ws, run)
    errs = q.errors(ws, run)
    state = detail["state"]
    hidden = {"run": run}
    buttons = []
    for key, (lbl, _) in RUN_ACTIONS.items():
        if key == "fetch":
            opts = "".join(f'<option value="{s}">{s}</option>' for s in FETCH_SOURCES)
            buttons.append(_form(token, key, lbl, hidden, f'<select name="source">{opts}</select> '))
        elif key == "clean":
            box = '<label><input type="checkbox" name="confirm" value="yes" required> confirmo</label> '
            buttons.append(_form(token, key, lbl, hidden, box, cls="warn"))
        else:
            buttons.append(_form(token, key, lbl, hidden))
    targets = sorted(TRANSITIONS.get(state, set()))
    move = ""
    if targets:
        opts = "".join(f'<option value="{_esc(t)}">{_esc(t)}</option>' for t in targets)
        need = " (obrigatória para sair de BLOCKED)" if state == "BLOCKED" else ""
        move = _form(
            token, "move", "move", hidden,
            f'<select name="to">{opts}</select><br><label>justificativa{need}</label>'
            '<textarea name="justification"></textarea><br>',
        )
    decide = _form(
        token, "decide", "registrar decisão", hidden,
        "<label>texto da decisão (escrito pela pesquisadora)</label><textarea name=\"text\" required></textarea><br>",
    )
    procs = q.process_events(ws, run)["events"][-100:]
    proc_rows = "".join(
        f"<tr><td>{_esc(e.get('ts'))}</td><td>{_esc(e.get('event'))}</td>"
        f"<td>{_esc((e.get('data') or {}).get('process'))}</td><td>{_esc((e.get('data') or {}).get('task_id'))}</td>"
        f"<td><code>{_esc(json.dumps(e.get('data'), ensure_ascii=False)[:300])}</code></td></tr>"
        for e in reversed(procs)
    )
    events = detail["ledger_events"][-50:]
    ev_rows = "".join(
        f"<tr><td>{_esc(e.get('ts'))}</td><td>{_esc(e.get('event'))}</td><td>{_esc(e.get('actor'))}</td>"
        f"<td><code>{_esc(json.dumps(e.get('data'), ensure_ascii=False)[:400])}</code></td></tr>"
        for e in reversed(events)
    )
    err_rows = "".join(
        f"<tr><td>{_esc(e.get('ts'))}</td><td><pre>{_json(e.get('data'))}</pre></td></tr>"
        for e in reversed(errs["errors"][-20:])
    )
    body = (
        _running_note(jobs)
        + f'<p>estado: <span class="state {_esc(state)}">{_esc(state)}</span></p>'
        + f"<fieldset><legend>comandos</legend>{''.join(buttons)}</fieldset>"
        + f"<fieldset><legend>ações da pesquisadora</legend>{move}{decide}</fieldset>"
        + f"<h3>processos do Nextflow (weblog, últimos {len(procs)})</h3>"
        + (
            "<table><tr><th>ts</th><th>evento</th><th>processo</th><th>task</th><th>dados</th></tr>"
            f"{proc_rows}</table>" if proc_rows else "<p>nenhum evento de processo</p>"
        )
        + "<h3>métricas</h3>"
        + (f"<pre>{_json(metrics['metrics'])}</pre>" if metrics["metrics"] is not None else "<p>sem métricas</p>")
        + _source(metrics["source"])
        + f"<h3>regras em uso</h3><pre>{_json(metrics['rules_in_use'])}</pre>"
        + _source(metrics["rules_source"])
        + "<h3>erros</h3>"
        + (f"<table><tr><th>ts</th><th>dados</th></tr>{err_rows}</table>" if err_rows else "<p>nenhum</p>")
        + f"<h3>eventos do ledger (últimos {len(events)})</h3>"
        + f"<table><tr><th>ts</th><th>evento</th><th>ator</th><th>dados</th></tr>{ev_rows}</table>"
        + _source(detail["source"])
    )
    return _page(ws, f"Corrida {run}", body, refresh=jobs.running() is not None)


def page_ledger(ws: Workspace) -> str:
    ver = q.ledger_verify(ws)
    tail = q.ledger_tail(ws, 100)
    rows = "".join(
        f"<tr><td>{_esc(e.get('ts'))}</td><td>{_esc(e.get('event'))}</td><td>{_esc(e.get('actor'))}</td>"
        f"<td><code>{_esc(json.dumps(e.get('data'), ensure_ascii=False)[:400])}</code></td>"
        f"<td><code>{_esc((e.get('hash') or '')[:12])}</code></td></tr>"
        for e in reversed(tail["entries"])
    )
    body = (
        f"<p>verify: ok={_esc(ver['ok'])} entradas={_esc(ver['entries'])} "
        f"quebra_na_linha={_esc(ver['broken_at_line'])} motivo={_esc(ver['reason'])}</p>"
        + f"<p>total de entradas: {_esc(tail['total_entries'])} (mostrando as últimas {len(tail['entries'])})</p>"
        + f"<table><tr><th>ts</th><th>evento</th><th>ator</th><th>dados</th><th>hash</th></tr>{rows}</table>"
        + _source(tail["source"])
    )
    return _page(ws, "Ledger", body)


def page_jobs(ws: Workspace, jobs: JobManager) -> str:
    with jobs.lock:
        items = list(reversed(jobs.jobs))
    rows = "".join(
        f'<tr><td><a href="/job/{_esc(j.id)}">{_esc(j.id)}</a></td><td><code>amplisentinel {_esc(" ".join(j.argv[2:]))}</code></td>'
        f"<td>{_esc(j.status)}</td><td>{_esc(j.exit_code)}</td></tr>"
        for j in items
    )
    body = (
        "<p>comandos iniciados nesta sessão da interface; o histórico completo está no ledger "
        "(eventos <code>ui_command</code> e <code>ui_command_finished</code>).</p>"
        + f"<table><tr><th>id</th><th>comando</th><th>situação</th><th>código de saída</th></tr>{rows}</table>"
    )
    return _page(ws, "Comandos", body, refresh=jobs.running() is not None)


def page_job(ws: Workspace, job: Job) -> str:
    data = job.log_path.read_bytes() if job.log_path.exists() else b""
    text = data[-200_000:].decode("utf-8", errors="replace")
    body = (
        f"<p><code>amplisentinel {_esc(' '.join(job.argv))}</code></p>"
        f"<p>situação: {_esc(job.status)}  código de saída: {_esc(job.exit_code)}  "
        f"início: {_esc(job.started)}  fim: {_esc(job.finished)}</p>"
        f"<h3>saída</h3><pre>{_esc(text)}</pre>"
        f'<p class="src">log: <code>{_esc(job.log_path)}</code>'
        + (f" sha256=<code>{_esc(job.log_sha256[:16])}</code>" if job.log_sha256 else "")
        + "</p>"
    )
    return _page(ws, f"Comando {job.id}", body, refresh=job.status == "running")


# ------------------------------------------------------------------ server


class UiServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = os.name != "nt"  # see listen.ExclusiveHTTPServer

    def __init__(self, ws: Workspace, port: int, launcher=default_launcher):
        super().__init__((HOST, port), Handler)
        self.ws = ws
        self.token = secrets.token_urlsafe(24)
        self.jobs = JobManager(ws, launcher)

    @property
    def port(self) -> int:
        return self.server_address[1]

    @property
    def url(self) -> str:
        return f"http://{HOST}:{self.port}/"

    def allowed_hosts(self) -> set[str]:
        return {f"127.0.0.1:{self.port}", f"localhost:{self.port}"}

    def allowed_origins(self) -> set[str]:
        return {f"http://{h}" for h in self.allowed_hosts()}


class Handler(BaseHTTPRequestHandler):
    server: UiServer
    server_version = "AmpliSentinelUI"

    def log_message(self, fmt: str, *args: Any) -> None:  # quiet
        return

    def _send(self, code: int, body: str, ctype: str = "text/html; charset=utf-8",
              headers: dict[str, str] | None = None) -> None:
        data = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'none'; style-src 'unsafe-inline'; img-src data:; form-action 'self'")
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _host_ok(self) -> bool:
        return self.headers.get("Host", "") in self.server.allowed_hosts()

    def _redirect(self, location: str) -> None:
        self._send(303, "", headers={"Location": location})

    def do_GET(self) -> None:
        if not self._host_ok():
            self._send(403, "host não permitido", "text/plain; charset=utf-8")
            return
        ws, srv = self.server.ws, self.server
        path = unquote(urlparse(self.path).path)
        try:
            if path == "/":
                self._send(200, page_home(ws, srv.token, srv.jobs))
            elif path.startswith("/run/"):
                self._send(200, page_run(ws, srv.token, srv.jobs, path[len("/run/"):]))
            elif path == "/ledger":
                self._send(200, page_ledger(ws))
            elif path == "/jobs":
                self._send(200, page_jobs(ws, srv.jobs))
            elif path.startswith("/job/"):
                job = srv.jobs.get(path[len("/job/"):])
                if job is None:
                    self._send(404, _page(ws, "Comando", "<p>comando não encontrado nesta sessão</p>"))
                else:
                    self._send(200, page_job(ws, job))
            elif path == "/report":
                text, _ = report.render_report_html(ws)
                self._send(200, text)
            else:
                self._send(404, _page(ws, "404", "<p>página não encontrada</p>"))
        except q.QueryError as exc:
            self._send(404, _page(ws, "Erro", f'<p class="msg">{_esc(exc)}</p>'))
        except Exception as exc:  # noqa: BLE001 (show the literal error, never hide it)
            self._send(500, _page(ws, "Erro", f"<pre>{_esc(type(exc).__name__)}: {_esc(exc)}</pre>"))

    do_HEAD = do_GET

    def do_POST(self) -> None:
        if not self._host_ok():
            self._send(403, "host não permitido", "text/plain; charset=utf-8")
            return
        origin = self.headers.get("Origin")
        if origin is not None and origin not in self.server.allowed_origins():
            self._send(403, "origem não permitida", "text/plain; charset=utf-8")
            return
        length = min(int(self.headers.get("Content-Length") or 0), 100_000)
        raw = self.rfile.read(length).decode("utf-8", errors="replace")
        form = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
        if not secrets.compare_digest(form.get("token", ""), self.server.token):
            self._send(403, "token inválido", "text/plain; charset=utf-8")
            return
        path = urlparse(self.path).path
        if not path.startswith("/action/"):
            self._send(404, "", "text/plain; charset=utf-8")
            return
        ws = self.server.ws
        try:
            args = build_args(ws, path[len("/action/"):], form)
            job = self.server.jobs.start(args)
        except UiRefused as exc:
            self._send(409, page_home(ws, self.server.token, self.server.jobs, msg=f"recusado: {exc}"))
            return
        self._redirect(f"/job/{job.id}")


def serve(ws: Workspace, port: int = DEFAULT_PORT, open_browser: bool = True,
          with_listener: bool = True) -> None:
    srv = UiServer(ws, port)
    print(f"AmpliSentinel UI: {srv.url}  workspace={ws.name}  (Ctrl+C para parar)", flush=True)
    weblog = None
    if with_listener:
        try:
            weblog = listen.make_server(ws)
        except OSError as exc:
            print(f"receptor weblog não iniciado: {exc}", flush=True)
        else:
            threading.Thread(target=weblog.serve_forever, daemon=True).start()
            host, wport = weblog.server_address[:2]
            print(f"receptor weblog: http://{host}:{wport}/events", flush=True)
    if open_browser:
        threading.Timer(0.5, lambda: webbrowser.open(srv.url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        srv.server_close()
        if weblog is not None:
            weblog.shutdown()
            weblog.server_close()
