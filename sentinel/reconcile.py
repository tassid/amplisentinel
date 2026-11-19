"""Reconciliation of FASTQ files: ENA (official MD5) x Google Drive (rclone).

For every accession of every run, the ENA record gives the expected FASTQ file
names and their MD5. The Drive listing (`rclone lsjson --hash --recursive`,
which downloads nothing) gives which files are present and their MD5.
One row per expected FASTQ file goes to .sentinel/inventory/reconciliation.tsv.

Values are copied, never interpreted. A run without `drive_path` in runs.yaml
is not looked up on Drive, so its rows say `not_checked`. Two files with the
same name in the Drive folder give `equal` = no, because the match is ambiguous.
"""

from __future__ import annotations

import csv
import hashlib
import json
import os
from dataclasses import dataclass
from pathlib import Path

from . import inventory as inv
from .ext import CallRecord, Runner, ToolError, call, subprocess_runner
from .workspace import Workspace, WorkspaceError

DRIVE_REMOTE = "gdrive_ro"
COLUMNS = [
    "run",
    "accession",
    "ena_found",
    "expected_file",
    "ena_md5",
    "ena_bytes",
    "present_in_drive",
    "drive_md5",
    "equal",
]


@dataclass(frozen=True)
class DriveFile:
    name: str
    md5: str
    path: str = ""


def _split(value: str | None) -> list[str]:
    return [v for v in (value or "").split(";") if v]


def expected_rows(run: str, accession: str, record: dict | None) -> list[dict]:
    """One row per FASTQ file named in the ENA record (ENA lists are ';' separated)."""
    base = {"run": run, "accession": accession}
    if record is None:
        return [
            {**base, "ena_found": "no", "expected_file": "", "ena_md5": "", "ena_bytes": ""}
        ]
    urls = _split(record.get("fastq_ftp"))
    md5s = _split(record.get("fastq_md5"))
    sizes = _split(record.get("fastq_bytes"))
    if not urls:
        return [
            {**base, "ena_found": "yes", "expected_file": "", "ena_md5": "", "ena_bytes": ""}
        ]
    rows = []
    for i, url in enumerate(urls):
        rows.append(
            {
                **base,
                "ena_found": "yes",
                "expected_file": url.rsplit("/", 1)[-1],
                "ena_md5": md5s[i] if i < len(md5s) else "",
                "ena_bytes": sizes[i] if i < len(sizes) else "",
            }
        )
    return rows


def parse_listing(stdout: bytes) -> list[DriveFile]:
    """Files from `rclone lsjson --hash` output. Folders are skipped."""
    items = json.loads(stdout or b"[]")
    files = []
    for item in items:
        if item.get("IsDir"):
            continue
        md5 = (item.get("Hashes") or {}).get("md5") or ""
        files.append(DriveFile(name=item["Name"], md5=md5.lower(), path=item.get("Path") or item["Name"]))
    return files


def list_drive(
    ws: Workspace, drive_path: str, runner: Runner, context: dict | None = None
) -> tuple[list[DriveFile], CallRecord]:
    """List a Drive folder with hashes. Nothing is downloaded."""
    argv = ["rclone", "lsjson", "--hash", "--recursive", f"{DRIVE_REMOTE}:{drive_path}"]
    record = call(ws, argv, version_argv=["rclone", "version"], runner=runner, context=context)
    try:
        files = parse_listing(record.stdout)
    except (json.JSONDecodeError, KeyError, TypeError) as exc:
        raise ToolError(
            f"rclone lsjson devolveu uma saída que não é a lista esperada ({exc}); "
            f"saída literal em {record.stdout_path} (sha256 {record.stdout_sha256})"
        ) from None
    return files, record


def compare(row: dict, drive_files: list[DriveFile] | None) -> dict:
    """Add the Drive columns to one expected row."""
    if drive_files is None:
        return {**row, "present_in_drive": "not_checked", "drive_md5": "", "equal": "not_checked"}
    name = row["expected_file"]
    matches = [f for f in drive_files if name and f.name == name]
    if not matches:
        return {**row, "present_in_drive": "no", "drive_md5": "", "equal": "no"}
    same = (
        len(matches) == 1
        and bool(matches[0].md5)
        and matches[0].md5 == (row["ena_md5"] or "").lower()
    )
    return {
        **row,
        "present_in_drive": "yes",
        "drive_md5": ";".join(f.md5 for f in matches),
        "equal": "yes" if same else "no",
    }


def _sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build(
    ws: Workspace,
    fetcher: inv.Fetcher = inv.ena_fetch,
    runner: Runner = subprocess_runner,
) -> dict:
    """Write reconciliation.tsv for all runs and record it in the ledger."""
    runs = ws.runs()
    accessions_by_run: dict[str, list[str]] = {}
    for name, cfg in runs.items():
        if not cfg.accessions:
            raise WorkspaceError(f"corrida {name}: falta 'accessions' em config/runs.yaml")
        path = ws.root / cfg.accessions
        if not path.exists():
            raise WorkspaceError(f"arquivo de accessions não encontrado para {name}: {path}")
        accessions_by_run[name] = inv.read_accessions(path)

    all_accs = sorted({a for accs in accessions_by_run.values() for a in accs})
    cache_path = ws.inventory_dir / "ena_cache.json"
    meta = inv.fetch_metadata(all_accs, cache_path, fetcher)

    rows: list[dict] = []
    drive_logs: dict[str, dict] = {}
    without_drive: list[str] = []
    for name, cfg in runs.items():
        drive_files: list[DriveFile] | None = None
        if cfg.drive_path:
            drive_files, record = list_drive(ws, cfg.drive_path, runner, context={"run": name})
            drive_logs[name] = {
                "drive_path": cfg.drive_path,
                "stdout": {"path": record.stdout_path, "sha256": record.stdout_sha256},
                "version": record.version,
            }
        else:
            without_drive.append(name)
        for acc in accessions_by_run[name]:
            for row in expected_rows(name, acc, meta.get(acc)):
                rows.append(compare(row, drive_files))

    out_path = ws.inventory_dir / "reconciliation.tsv"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(".tmp")
    with tmp.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS, delimiter="\t")
        writer.writeheader()
        writer.writerows(rows)
    os.replace(tmp, out_path)

    digest = _sha256_file(out_path)
    summary = {
        "runs": sorted(runs),
        "accessions": len(all_accs),
        "files": len(rows),
        "present_in_drive": sum(r["present_in_drive"] == "yes" for r in rows),
        "equal": sum(r["equal"] == "yes" for r in rows),
        "runs_without_drive_path": without_drive,
        "drive_listings": drive_logs,
        "output": {"path": str(out_path), "sha256": digest},
        "ena_cache": {"path": str(cache_path), "sha256": _sha256_file(cache_path)},
    }
    ws.ledger.append("reconciliation_built", summary)
    return summary
