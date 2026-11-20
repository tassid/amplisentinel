"""Stage 6: publish verified run outputs and safely clean local raw files.

`sentinel publish <RUN>`:
  Uploads `.qza`, `.qzv`, metrics, logs, and the run's ledger slice to
  `gdrive_rw:<workspace>/<RUN>/`, registers the bundle in DVC, logs the run in
  MLflow (including Git commit and ledger head hash), checks remote MD5s after
  upload, and transitions `MEASURED -> PUBLISHED`.

`sentinel clean <RUN>`:
  Runs strictly in `PUBLISHED`, re-checks remote MD5s against the published
  manifest before touching disk, deletes `runs/<RUN>/fastq/` and
  `runs/<RUN>/work/`, records every deleted file with its hash in the ledger,
  and transitions `PUBLISHED -> CLEANED`.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

from . import reconcile
from .common import (
    log_mlflow_run,
    md5_file,
    sha256_file,
    utc_iso,
    write_json_atomic,
    write_text_atomic,
)
from .ext import Runner, ToolError, call, subprocess_runner
from .ledger import git_commit
from .run import settings
from .workspace import Workspace

DRIVE_RW_REMOTE = "gdrive_rw"


class PublishRefused(Exception):
    """Publish could not start. The run state did not change."""


class CleanRefused(Exception):
    """Clean was refused. No local files were deleted."""


def _collect_artifacts(ws: Workspace, run: str, slice_path: Path) -> list[tuple[Path, str]]:
    run_dir = ws.root / "runs" / run
    items: list[tuple[Path, str]] = []
    seen: set[Path] = set()

    def add(path: Path, rel: str) -> None:
        rp = path.resolve()
        if path.exists() and path.is_file() and rp not in seen:
            seen.add(rp)
            items.append((path, rel.replace("\\", "/")))

    for pattern in ("*.qza", "*.qzv"):
        if run_dir.exists():
            for p in sorted(run_dir.rglob(pattern)):
                rel = p.relative_to(run_dir).as_posix()
                add(p, f"artifacts/{rel}")

    metrics_path = ws.metrics_dir / f"{run}.json"
    add(metrics_path, f"metrics/{run}.json")

    ampliseq_dir = run_dir / "ampliseq"
    if ampliseq_dir.exists():
        for p in sorted(ampliseq_dir.rglob("*")):
            if p.is_file():
                add(p, f"logs/ampliseq/{p.relative_to(ampliseq_dir).as_posix()}")

    logs_dir = ws.home / "logs"
    if logs_dir.exists():
        for p in sorted(logs_dir.iterdir()):
            if p.is_file():
                add(p, f"logs/ext/{p.name}")

    add(slice_path, "ledger_slice.jsonl")
    return items


def _write_ledger_slice(ws: Workspace, run: str, dest: Path) -> str:
    entries = [e for e in ws.ledger.entries() if (e.get("data") or {}).get("run") == run]
    text = "".join(json.dumps(e, ensure_ascii=False, sort_keys=True) + "\n" for e in entries)
    return write_text_atomic(dest, text)


def _list_remote_md5(ws: Workspace, run: str, remote_root: str, runner: Runner) -> dict[str, str]:
    rec = call(
        ws,
        ["rclone", "lsjson", "--hash", "--recursive", remote_root],
        version_argv=["rclone", "version"],
        runner=runner,
        context={"run": run},
    )
    files = reconcile.parse_listing(rec.stdout)
    out: dict[str, str] = {}
    for f in files:
        key = (f.path or f.name).replace("\\", "/").lstrip("/")
        if f.md5:
            out[key] = f.md5.lower()
    return out


def publish_run(
    ws: Workspace,
    run: str,
    *,
    runner: Runner = subprocess_runner,
) -> dict[str, Any]:
    """Upload outputs to gdrive_rw, register in DVC and MLflow, and verify remote MD5s."""
    runs = ws.runs()
    if run not in runs:
        raise PublishRefused(f"corrida desconhecida neste workspace: {run}")
    rc = runs[run]

    current = ws.state.get(run)
    if current == "BLOCKED":
        raise PublishRefused(
            f"{run} está em BLOCKED; sair de BLOCKED exige `sentinel move {run} MEASURED -j`"
        )
    if current != "MEASURED":
        raise PublishRefused(f"{run} está em {current}; publish só roda em MEASURED")

    run_dir = ws.root / "runs" / run
    publish_dir = run_dir / "publish"
    publish_dir.mkdir(parents=True, exist_ok=True)
    slice_path = publish_dir / "ledger_slice.jsonl"
    slice_sha = _write_ledger_slice(ws, run, slice_path)

    items = _collect_artifacts(ws, run, slice_path)
    if not items:
        raise PublishRefused(f"corrida {run}: nenhum artefato para publicar")

    remote_root = f"{DRIVE_RW_REMOTE}:{ws.name}/{run}"
    staged: list[dict[str, Any]] = []
    for local_path, rel in items:
        staged.append(
            {
                "rel_path": rel,
                "local_path": str(local_path),
                "bytes": local_path.stat().st_size,
                "md5": md5_file(local_path),
                "sha256": sha256_file(local_path),
            }
        )

    manifest_path = run_dir / "publish_manifest.json"
    ctx = {"run": run, "stage": "publish"}

    try:
        for entry in staged:
            call(
                ws,
                ["rclone", "copyto", entry["local_path"], f"{remote_root}/{entry['rel_path']}"],
                version_argv=["rclone", "version"],
                runner=runner,
                context=ctx,
            )
        remote_md5 = _list_remote_md5(ws, run, remote_root, runner)
    except ToolError as exc:
        ws.state.move(run, "BLOCKED")
        return {
            "run": run,
            "state": "BLOCKED",
            "reason": str(exc),
            "error_log": exc.log,
        }

    mismatches: list[dict[str, Any]] = []
    for entry in staged:
        got = remote_md5.get(entry["rel_path"])
        entry["remote_md5"] = got
        entry["equal"] = got == entry["md5"]
        if not entry["equal"]:
            mismatches.append(
                {
                    "rel_path": entry["rel_path"],
                    "expected_md5": entry["md5"],
                    "remote_md5": got,
                }
            )

    if mismatches:
        manifest = {
            "run": run,
            "generated_at": utc_iso(),
            "remote_root": remote_root,
            "result": "BLOCKED",
            "files": staged,
            "mismatches": mismatches,
        }
        m_sha = write_json_atomic(manifest_path, manifest)
        ws.ledger.append(
            "error",
            {"run": run, "stage": "publish", "reason": "hash remoto divergente", "mismatches": mismatches},
        )
        ws.state.move(run, "BLOCKED")
        return {
            "run": run,
            "state": "BLOCKED",
            "mismatches": mismatches,
            "manifest": {"path": str(manifest_path), "sha256": m_sha},
        }

    bundle_index = publish_dir / "bundle.json"
    bundle_sha = write_json_atomic(
        bundle_index,
        {"run": run, "remote_root": remote_root, "files": staged},
    )
    try:
        call(
            ws,
            ["dvc", "add", str(bundle_index)],
            version_argv=["dvc", "version"],
            runner=runner,
            context=ctx,
        )
        call(
            ws,
            ["dvc", "push", str(bundle_index.with_suffix(".json.dvc"))],
            version_argv=["dvc", "version"],
            runner=runner,
            context=ctx,
        )
    except ToolError as exc:
        ws.state.move(run, "BLOCKED")
        return {
            "run": run,
            "state": "BLOCKED",
            "reason": str(exc),
            "error_log": exc.log,
        }

    dvc_file = bundle_index.with_suffix(".json.dvc")
    dvc_info = {
        "bundle": {"path": str(bundle_index), "sha256": bundle_sha},
        "dvc_file": {"path": str(dvc_file), "sha256": sha256_file(dvc_file)} if dvc_file.exists() else None,
    }

    metrics_path = ws.metrics_dir / f"{run}.json"
    numeric_metrics: dict[str, float] = {}
    if metrics_path.exists():
        m_data = json.loads(metrics_path.read_text(encoding="utf-8"))
        for k, v in (m_data.get("values") or {}).items():
            if isinstance(v, dict) and isinstance(v.get("value"), (int, float)):
                numeric_metrics[k.replace(".", "_")] = float(v["value"])

    cfg = settings(ws)
    head_entries = ws.ledger.entries()
    ledger_head = head_entries[-1]["hash"] if head_entries else ("0" * 64)
    mlflow_info = log_mlflow_run(
        ws,
        run_name=f"{run}-published",
        params={
            "run": run,
            "region": rc.region or "",
            "ampliseq_release": cfg.ampliseq_release,
            "git_commit": git_commit(ws.root) or "unknown",
            "ledger_head": ledger_head,
            "ledger_slice_sha256": slice_sha,
        },
        metrics=numeric_metrics,
        tags={"run": run, "stage": "publish", "git_commit": git_commit(ws.root) or "unknown", "ledger_head": ledger_head},
        artifacts=[p for p in (metrics_path, slice_path) if p.exists()],
    )

    manifest = {
        "run": run,
        "generated_at": utc_iso(),
        "remote_root": remote_root,
        "result": "PUBLISHED",
        "ledger_slice": {"path": str(slice_path), "sha256": slice_sha},
        "dvc": dvc_info,
        "mlflow": mlflow_info,
        "files": staged,
    }
    m_sha = write_json_atomic(manifest_path, manifest)
    ws.ledger.append(
        "published",
        {
            "run": run,
            "remote_root": remote_root,
            "files": len(staged),
            "manifest": {"path": str(manifest_path), "sha256": m_sha},
            "dvc": dvc_info,
            "mlflow": mlflow_info,
        },
    )
    ws.state.move(run, "PUBLISHED")
    return {
        "run": run,
        "state": "PUBLISHED",
        "files": len(staged),
        "remote_root": remote_root,
        "manifest": {"path": str(manifest_path), "sha256": m_sha},
        "mlflow": mlflow_info,
    }


def clean_run(
    ws: Workspace,
    run: str,
    *,
    runner: Runner = subprocess_runner,
) -> dict[str, Any]:
    """Re-verify remote hashes and remove local FASTQ and work/ only in PUBLISHED."""
    runs = ws.runs()
    if run not in runs:
        raise CleanRefused(f"corrida desconhecida neste workspace: {run}")

    current = ws.state.get(run)
    if current != "PUBLISHED":
        raise CleanRefused(f"{run} está em {current}; clean só roda em PUBLISHED")

    run_dir = ws.root / "runs" / run
    manifest_path = run_dir / "publish_manifest.json"
    if not manifest_path.exists():
        raise CleanRefused(f"corrida {run}: falta {manifest_path}")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if manifest.get("result") != "PUBLISHED" or not manifest.get("files"):
        raise CleanRefused(f"corrida {run}: publish_manifest.json não confirma publicação")

    remote_root = manifest["remote_root"]
    try:
        remote_md5 = _list_remote_md5(ws, run, remote_root, runner)
    except ToolError as exc:
        raise CleanRefused(f"falha ao conferir hashes remotos antes de apagar: {exc}") from None

    mismatches = [
        {
            "rel_path": entry["rel_path"],
            "expected_md5": entry["md5"],
            "remote_md5": remote_md5.get(entry["rel_path"]),
        }
        for entry in manifest["files"]
        if remote_md5.get(entry["rel_path"]) != entry["md5"]
    ]
    if mismatches:
        ws.ledger.append(
            "error",
            {"run": run, "stage": "clean", "reason": "hash remoto divergente", "mismatches": mismatches},
        )
        raise CleanRefused(
            f"corrida {run}: hash remoto divergente ou arquivo ausente ({len(mismatches)}); nada foi apagado"
        )

    deleted: list[dict[str, Any]] = []
    for folder_name in ("fastq", "work"):
        folder = run_dir / folder_name
        if not folder.exists():
            continue
        for p in sorted(folder.rglob("*")):
            if p.is_file():
                deleted.append(
                    {
                        "path": str(p),
                        "bytes": p.stat().st_size,
                        "md5": md5_file(p),
                        "sha256": sha256_file(p),
                    }
                )
        shutil.rmtree(folder)

    clean_manifest_path = run_dir / "clean_manifest.json"
    cm_sha = write_json_atomic(
        clean_manifest_path,
        {
            "run": run,
            "generated_at": utc_iso(),
            "remote_root": remote_root,
            "deleted_files": deleted,
        },
    )
    ws.ledger.append(
        "cleaned",
        {
            "run": run,
            "deleted_count": len(deleted),
            "deleted_files": deleted,
            "manifest": {"path": str(clean_manifest_path), "sha256": cm_sha},
        },
    )
    ws.state.move(run, "CLEANED")
    return {
        "run": run,
        "state": "CLEANED",
        "deleted_count": len(deleted),
        "manifest": {"path": str(clean_manifest_path), "sha256": cm_sha},
    }
