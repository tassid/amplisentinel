"""Fetch worker: download the FASTQ files of one run and check them against ENA.

`amplisentinel fetch <RUN>`:

1. Reads the run's accessions and the ENA records (official FASTQ names, MD5
   and sizes; cached in .amplisentinel/inventory/ena_cache.json).
2. Checks free disk space: bytes still to download x `fetch.disk_slack`
   (amplisentinel.yaml). Too little space refuses the fetch with no state change.
3. Moves the run PENDING -> FETCHING (a run already in FETCHING is resumed).
4. Gets each file into <workspace>/runs/<RUN>/fastq/:
   - from Google Drive with `rclone copyto gdrive_ro:...` when the run has a
     `drive_path` and exactly one file with that name is there;
   - otherwise from ENA over HTTPS, into `<file>.part`, resumed with an HTTP
     Range request when the .part file already exists;
   - or, with `--via fetchngs`, with nf-core/fetchngs at the release pinned in
     amplisentinel.yaml. AmpliSentinel still checks every MD5 against ENA.
5. Computes the local MD5 of every file and compares it with ENA.
   All equal -> MD5_OK. Any difference or failure -> BLOCKED, with an `error`
   event listing the files and the literal error logs.
6. Writes runs/<RUN>/fetch_manifest.json and records its sha256 in the ledger.

Nothing is deleted or overwritten here. A file already present is checked as
it is; if its MD5 differs from ENA, the run is BLOCKED and the file stays.
"""

from __future__ import annotations

import hashlib
import http.client
import json
import math
import os
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from . import inventory as inv
from . import reconcile
from .ext import Runner, ToolError, call, save_log, subprocess_runner
from .runs import ConfigError
from .workspace import Workspace

DEFAULT_DISK_SLACK = 1.2
FETCH_KEYS = ("disk_slack", "fetchngs_release")
ENGINES = ("docker", "podman")
CHUNK = 1024 * 1024

Downloader = Callable[[str, Path], None]
FreeBytes = Callable[[Path], int]


class FetchRefused(Exception):
    """The fetch did not start. The run state did not change."""


class DownloadError(Exception):
    pass


# Failures of network and file I/O that are recorded literally instead of raised.
TRANSFER_ERRORS = (OSError, ValueError, http.client.HTTPException, DownloadError)


@dataclass(frozen=True)
class FastqFile:
    accession: str
    name: str
    url: str
    md5: str
    bytes: int


@dataclass(frozen=True)
class FetchSettings:
    disk_slack: float
    fetchngs_release: str | None
    container_engine: str


def settings(ws: Workspace) -> FetchSettings:
    """Fetch settings from amplisentinel.yaml. Unknown keys in `fetch` are refused."""
    meta = ws.meta
    block = meta.get("fetch") or {}
    if not isinstance(block, dict):
        raise ConfigError("amplisentinel.yaml: 'fetch' precisa ser um mapa")
    unknown = sorted(str(k) for k in block if k not in FETCH_KEYS)
    if unknown:
        raise ConfigError(f"amplisentinel.yaml fetch: chave desconhecida {unknown}; aceitas: {', '.join(FETCH_KEYS)}")
    slack = block.get("disk_slack", DEFAULT_DISK_SLACK)
    if isinstance(slack, bool) or not isinstance(slack, (int, float)) or slack < 1:
        raise ConfigError("amplisentinel.yaml fetch.disk_slack: esperado número >= 1")
    release = block.get("fetchngs_release")
    if release is not None and (not isinstance(release, str) or not release.strip()):
        raise ConfigError("amplisentinel.yaml fetch.fetchngs_release: esperado texto")
    engine = meta.get("container_engine", "docker")
    if engine not in ENGINES:
        raise ConfigError(f"amplisentinel.yaml container_engine: aceitos {', '.join(ENGINES)}")
    return FetchSettings(float(slack), release, engine)


def http_download(url: str, dest: Path) -> None:
    """Download url into dest, continuing from dest's current size (HTTP Range).

    A server that ignores the Range request raises DownloadError and the
    partial file is kept as it is.
    """
    offset = dest.stat().st_size if dest.exists() else 0
    headers = {"Range": f"bytes={offset}-"} if offset else {}
    req = urllib.request.Request(url, headers=headers)
    try:
        resp = urllib.request.urlopen(req, timeout=120)
    except urllib.error.HTTPError as exc:
        if exc.code == 416 and offset:
            return  # nothing left to send; the MD5 check decides
        raise
    with resp:
        if offset and resp.status != 206:
            raise DownloadError(
                f"servidor respondeu HTTP {resp.status} a um pedido de retomada; "
                f"arquivo parcial mantido em {dest}"
            )
        with dest.open("ab") as fh:
            shutil.copyfileobj(resp, fh, CHUNK)


def disk_free(path: Path) -> int:
    return shutil.disk_usage(path).free


def md5_file(path: Path) -> str:
    h = hashlib.md5(usedforsecurity=False)
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def _split(value: str | None) -> list[str]:
    return [v for v in (value or "").split(";") if v]


def plan(accessions: list[str], meta: dict[str, dict]) -> tuple[list[FastqFile], list[dict]]:
    """Expected files from the ENA records, and problems found in those records."""
    files: list[FastqFile] = []
    problems: list[dict] = []
    for acc in accessions:
        rec = meta.get(acc)
        if rec is None:
            problems.append({"accession": acc, "file": None, "reason": "accession não encontrado no ENA"})
            continue
        urls, md5s, sizes = _split(rec.get("fastq_ftp")), _split(rec.get("fastq_md5")), _split(rec.get("fastq_bytes"))
        if not urls:
            problems.append({"accession": acc, "file": None, "reason": "ENA não lista FASTQ (fastq_ftp vazio)"})
            continue
        if len(md5s) != len(urls) or len(sizes) != len(urls):
            problems.append(
                {
                    "accession": acc,
                    "file": None,
                    "reason": f"ENA lista {len(urls)} arquivos, {len(md5s)} MD5 e {len(sizes)} tamanhos",
                }
            )
            continue
        for url, md5, size in zip(urls, md5s, sizes):
            name = url.rsplit("/", 1)[-1]
            files.append(FastqFile(acc, name, "https://" + url.split("://", 1)[-1], md5.lower(), int(size)))
    return files, problems


def _local_size(path: Path) -> int:
    return path.stat().st_size if path.exists() else 0


def _write_json_atomic(path: Path, data: dict) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _via_fetchngs(
    ws: Workspace,
    run: str,
    files: list[FastqFile],
    fastq_dir: Path,
    cfg: FetchSettings,
    runner: Runner,
) -> tuple[dict[str, dict], list[dict]]:
    """Run nf-core/fetchngs and move its FASTQ files into fastq_dir."""
    workdir = ws.root / "runs" / run / "fetchngs"
    outdir = workdir / "results"
    workdir.mkdir(parents=True, exist_ok=True)
    ids = workdir / "ids.csv"
    ids.write_text("".join(f"{a}\n" for a in sorted({f.accession for f in files})), encoding="utf-8")
    argv = [
        "nextflow",
        "-log", str(workdir / "nextflow.log"),
        "run", "nf-core/fetchngs",
        "-r", cfg.fetchngs_release or "",
        "-profile", cfg.container_engine,
        "-work-dir", str(workdir / "work"),
        "--input", str(ids),
        "--outdir", str(outdir),
        "-resume",
    ]
    origin: dict[str, dict] = {}
    try:
        call(ws, argv, version_argv=["nextflow", "-version"], runner=runner, context={"run": run})
    except ToolError as exc:
        return origin, [{"accession": None, "file": None, "reason": str(exc), "log": exc.log}]
    problems: list[dict] = []
    produced = sorted((outdir / "fastq").glob("*.fastq.gz")) if (outdir / "fastq").exists() else []
    for f in files:
        final = fastq_dir / f.name
        if final.exists():
            continue
        found = [p for p in produced if p.name == f.name or p.name.endswith("_" + f.name)]
        if len(found) != 1:
            problems.append(
                {"accession": f.accession, "file": f.name,
                 "reason": f"fetchngs produziu {len(found)} arquivos com esse nome"}
            )
            continue
        os.replace(found[0], final)
        origin[f.name] = {"source": "fetchngs", "location": str(found[0])}
    return origin, problems


def run_fetch(
    ws: Workspace,
    run: str,
    *,
    via: str = "direct",
    source: str = "auto",
    fetcher: inv.Fetcher = inv.ena_fetch,
    runner: Runner = subprocess_runner,
    downloader: Downloader = http_download,
    free_bytes: FreeBytes = disk_free,
) -> dict:
    """Fetch and check one run. Returns a summary; raises FetchRefused before any state change."""
    if via not in ("direct", "fetchngs"):
        raise FetchRefused(f"--via aceita direct ou fetchngs, não {via}")
    if source not in ("auto", "drive", "ena"):
        raise FetchRefused(f"--source aceita auto, drive ou ena, não {source}")
    runs = ws.runs()
    if run not in runs:
        raise FetchRefused(f"corrida desconhecida neste workspace: {run}")
    rc = runs[run]
    if not rc.accessions:
        raise FetchRefused(f"corrida {run}: falta 'accessions' em config/runs.yaml")
    acc_path = ws.root / rc.accessions
    if not acc_path.exists():
        raise FetchRefused(f"arquivo de accessions não encontrado para {run}: {acc_path}")
    if via == "direct" and source == "drive" and not rc.drive_path:
        raise FetchRefused(f"corrida {run}: --source drive sem drive_path em config/runs.yaml")
    cfg = settings(ws)
    if via == "fetchngs" and not cfg.fetchngs_release:
        raise FetchRefused("--via fetchngs exige fetch.fetchngs_release em amplisentinel.yaml (release travada)")

    current = ws.state.get(run)
    if current == "BLOCKED":
        raise FetchRefused(f"{run} está em BLOCKED; sair de BLOCKED exige `amplisentinel move {run} FETCHING -j`")
    if current not in ("PENDING", "FETCHING"):
        raise FetchRefused(f"{run} está em {current}; fetch só roda em PENDING ou FETCHING")

    accessions = inv.read_accessions(acc_path)
    try:
        meta = inv.fetch_metadata(accessions, ws.inventory_dir / "ena_cache.json", fetcher)
    except TRANSFER_ERRORS as exc:  # network or ENA failure, kept literally
        log = save_log(ws, "ena-metadata.stderr", f"{type(exc).__name__}: {exc}".encode())
        ws.ledger.append("error", {"run": run, "stage": "fetch", "reason": "metadados do ENA", "log": log})
        raise FetchRefused(f"falha ao consultar o ENA; texto literal em {log['path']}") from None
    files, problems = plan(accessions, meta)

    fastq_dir = ws.root / "runs" / run / "fastq"
    fastq_dir.mkdir(parents=True, exist_ok=True)
    total = sum(f.bytes for f in files)
    remaining = sum(
        max(f.bytes - _local_size(fastq_dir / f"{f.name}.part"), 0)
        for f in files
        if not (fastq_dir / f.name).exists()
    )
    required = math.ceil(remaining * cfg.disk_slack)
    free = free_bytes(fastq_dir)
    disk = {"bytes_total": total, "bytes_remaining": remaining, "disk_slack": cfg.disk_slack,
            "disk_required": required, "disk_free": free}
    if free < required:
        ws.ledger.append("error", {"run": run, "stage": "fetch", "reason": "espaço em disco", **disk})
        raise FetchRefused(f"espaço livre {free} bytes; necessário {required} bytes ({remaining} x {cfg.disk_slack})")

    if current == "PENDING":
        ws.state.move(run, "FETCHING")
    ws.ledger.append(
        "fetch_started",
        {"run": run, "via": via, "source": source, "files": len(files),
         "fastq_dir": str(fastq_dir), "resumed": current == "FETCHING", **disk},
    )

    origin: dict[str, dict] = {}
    if via == "fetchngs":
        got, extra = _via_fetchngs(ws, run, files, fastq_dir, cfg, runner)
        origin.update(got)
        problems.extend(extra)
    else:
        drive: list[reconcile.DriveFile] | None = None
        listing_failed = False
        if rc.drive_path and source in ("auto", "drive") and any(not (fastq_dir / f.name).exists() for f in files):
            try:
                drive, _ = reconcile.list_drive(ws, rc.drive_path, runner, context={"run": run})
            except ToolError as exc:
                listing_failed = True
                problems.append({"accession": None, "file": None, "reason": str(exc), "log": exc.log})
        for f in files:
            final = fastq_dir / f.name
            if final.exists() or listing_failed:
                continue
            matches = [d for d in (drive or []) if d.name == f.name]
            use_drive = source == "drive" or (source == "auto" and len(matches) > 0)
            if use_drive:
                if len(matches) != 1:
                    problems.append({"accession": f.accession, "file": f.name,
                                     "reason": f"{len(matches)} arquivos com esse nome no Drive"})
                    continue
                remote = f"{reconcile.DRIVE_REMOTE}:{rc.drive_path}/{matches[0].path}"
                try:
                    call(ws, ["rclone", "copyto", remote, str(final)],
                         version_argv=["rclone", "version"], runner=runner, context={"run": run})
                    origin[f.name] = {"source": "drive", "location": remote}
                except ToolError as exc:
                    problems.append({"accession": f.accession, "file": f.name, "reason": str(exc), "log": exc.log})
                continue
            part = fastq_dir / f"{f.name}.part"
            try:
                downloader(f.url, part)
            except TRANSFER_ERRORS as exc:  # kept literally in a log file
                log = save_log(ws, "ena-download.stderr", f"{f.url}\n{type(exc).__name__}: {exc}".encode())
                problems.append({"accession": f.accession, "file": f.name,
                                 "reason": "download do ENA interrompido", "log": log})
                continue
            os.replace(part, final)
            origin[f.name] = {"source": "ena", "location": f.url}

    entries: list[dict] = []
    for f in files:
        final = fastq_dir / f.name
        got = origin.get(f.name, {"source": "local" if final.exists() else None, "location": str(final)})
        obtained = md5_file(final) if final.exists() else None
        entry = {
            "file": f.name,
            "accession": f.accession,
            "source": got["source"],
            "location": got["location"],
            "bytes_expected": f.bytes,
            "bytes": final.stat().st_size if final.exists() else None,
            "md5_expected": f.md5,
            "md5_obtained": obtained,
            "equal": obtained == f.md5,
        }
        entries.append(entry)
        if obtained is not None:
            ws.ledger.append("file_verified", {"run": run, **{k: entry[k] for k in (
                "file", "source", "bytes", "md5_expected", "md5_obtained", "equal")}})
            if obtained != f.md5:
                problems.append({"accession": f.accession, "file": f.name, "reason": "MD5 local diferente do ENA"})
        elif not any(p.get("file") == f.name for p in problems):
            problems.append({"accession": f.accession, "file": f.name, "reason": "arquivo não obtido"})

    to = "MD5_OK" if not problems and entries else "BLOCKED"
    if not entries and not problems:
        problems.append({"accession": None, "file": None, "reason": "nenhum FASTQ listado para a corrida"})
    manifest_path = ws.root / "runs" / run / "fetch_manifest.json"
    manifest = {
        "run": run,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "via": via,
        "source": source,
        "files": entries,
        "problems": problems,
    }
    digest = _write_json_atomic(manifest_path, manifest)
    ws.ledger.append(
        "fetch_completed",
        {"run": run, "manifest": {"path": str(manifest_path), "sha256": digest},
         "files": len(entries), "equal": sum(e["equal"] for e in entries), "result": to},
    )
    if to == "BLOCKED":
        ws.ledger.append("error", {"run": run, "stage": "fetch", "files": problems})
    ws.state.move(run, to)
    return {
        "run": run,
        "state": to,
        "files": len(entries),
        "equal": sum(e["equal"] for e in entries),
        "problems": problems,
        "manifest": {"path": str(manifest_path), "sha256": digest},
    }
