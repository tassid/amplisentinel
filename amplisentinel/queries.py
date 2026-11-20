"""Read-only queries over one workspace.

Shared by the CLI and the MCP server. Every function returns plain data with
the source file and its sha256, so any value can be traced back. Nothing here
writes, and nothing here interprets.
"""

from __future__ import annotations

import csv
import hashlib
import json
from pathlib import Path

from .workspace import Workspace


class QueryError(Exception):
    pass


def _source(path: Path) -> dict:
    if not path.exists():
        return {"path": str(path), "exists": False}
    return {
        "path": str(path),
        "exists": True,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _check_run(ws: Workspace, run: str) -> None:
    known = set(ws.state.all()) | set(ws.configured_runs())
    if run not in known:
        raise QueryError(f"corrida desconhecida neste workspace: {run}")


def workspace_info(ws: Workspace) -> dict:
    meta = ws.meta
    return {
        "workspace": ws.name,
        "title": meta.get("title"),
        "description": meta.get("description"),
        "runs_configured": ws.configured_runs(),
        "sources": [_source(ws.root / "amplisentinel.yaml"), _source(ws.runs_file)],
    }


def status(ws: Workspace) -> dict:
    states = ws.state.all()
    counts: dict[str, int] = {}
    for st in states.values():
        counts[st] = counts.get(st, 0) + 1
    return {
        "workspace": ws.name,
        "runs": dict(sorted(states.items())),
        "counts_by_state": counts,
        "source": _source(ws.state.path),
    }


def run_detail(ws: Workspace, run: str) -> dict:
    _check_run(ws, run)
    events = [
        e for e in ws.ledger.entries()
        if (e.get("data") or {}).get("run") == run
    ]
    return {
        "workspace": ws.name,
        "run": run,
        "state": ws.state.get(run),
        "ledger_events": events,
        "source": _source(ws.ledger.path),
    }


def run_metrics(ws: Workspace, run: str) -> dict:
    _check_run(ws, run)
    path = ws.metrics_dir / f"{run}.json"
    values = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return {
        "workspace": ws.name,
        "run": run,
        "metrics": values,
        "rules_in_use": ws.rules(),
        "source": _source(path),
        "rules_source": _source(ws.rules_file),
    }


def inventory(ws: Workspace, study: str | None = None) -> dict:
    path = ws.inventory_dir / "projects.tsv"
    rows: list[dict] = []
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
    if study is not None:
        rows = [r for r in rows if r.get("project") == study]
    return {"workspace": ws.name, "studies": rows, "source": _source(path)}


def ledger_tail(ws: Workspace, n: int = 20) -> dict:
    n = max(1, min(int(n), 500))
    entries = ws.ledger.entries()
    return {
        "workspace": ws.name,
        "total_entries": len(entries),
        "entries": entries[-n:],
        "source": _source(ws.ledger.path),
    }


def ledger_verify(ws: Workspace) -> dict:
    res = ws.ledger.verify()
    return {
        "workspace": ws.name,
        "ok": res.ok,
        "entries": res.entries,
        "broken_at_line": res.broken_at,
        "reason": res.reason,
        "source": _source(ws.ledger.path),
    }


def errors(ws: Workspace, run: str | None = None) -> dict:
    if run is not None:
        _check_run(ws, run)
    out = []
    for e in ws.ledger.entries():
        if e.get("event") != "error":
            continue
        if run is not None and (e.get("data") or {}).get("run") != run:
            continue
        out.append(e)
    return {"workspace": ws.name, "run": run, "errors": out, "source": _source(ws.ledger.path)}


def reconciliation(ws: Workspace, run: str | None = None) -> dict:
    if run is not None:
        _check_run(ws, run)
    path = ws.inventory_dir / "reconciliation.tsv"
    rows: list[dict] = []
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            rows = list(csv.DictReader(fh, delimiter="\t"))
    if run is not None:
        rows = [r for r in rows if r.get("run") == run]
    return {"workspace": ws.name, "run": run, "rows": rows, "source": _source(path)}


def fetch_manifest(ws: Workspace, run: str) -> dict:
    _check_run(ws, run)
    path = ws.root / "runs" / run / "fetch_manifest.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return {"workspace": ws.name, "run": run, "manifest": data, "source": _source(path)}


def process_events(ws: Workspace, run: str) -> dict:
    _check_run(ws, run)
    wanted = {"process_started", "process_completed", "process_failed"}
    events = [
        e for e in ws.ledger.entries()
        if e.get("event") in wanted and (e.get("data") or {}).get("run") == run
    ]
    return {"workspace": ws.name, "run": run, "events": events, "source": _source(ws.ledger.path)}


def between_metrics(ws: Workspace) -> dict:
    path = ws.metrics_dir / "between.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return {"workspace": ws.name, "between": data, "source": _source(path)}


def folds(ws: Workspace) -> dict:
    path = ws.home / "ml" / "folds.json"
    data = json.loads(path.read_text(encoding="utf-8")) if path.exists() else None
    return {"workspace": ws.name, "folds": data, "source": _source(path)}
