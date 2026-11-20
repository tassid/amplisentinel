"""Shared low-level utilities for hashing, atomic file writes, and MLflow logging."""

from __future__ import annotations

import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .ledger import git_commit
from .workspace import Workspace

CHUNK = 1024 * 1024


def utc_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")


def utc_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def md5_file(path: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def write_text_atomic(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return sha256_file(path)


def write_json_atomic(path: Path, data: Any) -> str:
    return write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))


def file_info(path: Path) -> dict | None:
    if not path.exists():
        return None
    return {"path": str(path), "sha256": sha256_file(path)}


def mlflow_tracking_uri(ws: Workspace) -> str:
    ws.home.mkdir(parents=True, exist_ok=True)
    return f"sqlite:///{(ws.home / 'mlflow.db').resolve().as_posix()}"


def log_mlflow_run(
    ws: Workspace,
    *,
    run_name: str,
    params: dict[str, Any],
    metrics: dict[str, float] | None = None,
    tags: dict[str, str] | None = None,
    artifacts: list[Path] | None = None,
) -> dict | None:
    """Record one run in the workspace's local MLflow tracking store.

    Returns tracking metadata or None if mlflow is not installed in the Python
    environment.
    """
    try:
        import mlflow
    except ImportError:
        return None

    mlruns_dir = ws.home / "mlruns"
    mlruns_dir.mkdir(parents=True, exist_ok=True)
    tracking_uri = mlflow_tracking_uri(ws)
    mlflow.set_tracking_uri(tracking_uri)
    if mlflow.get_experiment_by_name(ws.name) is None:
        mlflow.create_experiment(ws.name, artifact_location=mlruns_dir.resolve().as_uri())
    mlflow.set_experiment(ws.name)

    entries = ws.ledger.entries()
    ledger_head = entries[-1]["hash"] if entries else ("0" * 64)
    all_tags = {
        "workspace": ws.name,
        "git_commit": git_commit(ws.root) or "unknown",
        "ledger_head": ledger_head,
        **(tags or {}),
    }

    with mlflow.start_run(run_name=run_name) as active:
        mlflow.set_tags({k: str(v) for k, v in all_tags.items() if v is not None})
        if params:
            mlflow.log_params({str(k): str(v) for k, v in params.items()})
        if metrics:
            clean_metrics = {
                str(k): float(v)
                for k, v in metrics.items()
                if isinstance(v, (int, float)) and not isinstance(v, bool)
            }
            if clean_metrics:
                mlflow.log_metrics(clean_metrics)
        for art in artifacts or []:
            if art.exists() and art.is_file():
                mlflow.log_artifact(str(art))
        run_id = active.info.run_id

    return {
        "tracking_uri": tracking_uri,
        "experiment": ws.name,
        "run_id": run_id,
        "run_name": run_name,
    }
