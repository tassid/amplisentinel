"""Inventory: accession lists x ENA metadata.

Reads one accession list per project (files named <PROJECT>_runs.txt, one
SRR/ERR/DRR accession per line), asks the ENA Portal API for the official
metadata of each accession (layout, instrument, read count, FASTQ MD5 and
size) and writes two tables:

  inventory.tsv   one line per accession
  projects.tsv    one line per project, with counts only

Nothing here interprets the data. Values are copied from ENA as returned.
ENA responses are cached so the inventory can be rebuilt offline and the
cache itself can be hashed into the ledger.
"""

from __future__ import annotations

import csv
import hashlib
import json
import re
import urllib.parse
import urllib.request
from collections import defaultdict
from collections.abc import Callable, Iterable
from pathlib import Path

ENA_SEARCH = "https://www.ebi.ac.uk/ena/portal/api/search"
ENA_FIELDS = [
    "run_accession",
    "study_accession",
    "sample_accession",
    "library_layout",
    "library_strategy",
    "library_source",
    "instrument_platform",
    "instrument_model",
    "read_count",
    "base_count",
    "fastq_ftp",
    "fastq_md5",
    "fastq_bytes",
    "first_public",
]
ACCESSION_RE = re.compile(r"^[SED]RR\d+$")
BATCH = 100

Fetcher = Callable[[list[str]], list[dict]]


def read_accessions(path: Path) -> list[str]:
    """Accessions in one list file, one per line. Other lines are ignored."""
    accs = []
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        token = line.strip()
        if token and ACCESSION_RE.match(token):
            accs.append(token)
    return accs


def read_accession_lists(folder: Path) -> dict[str, list[str]]:
    """Return {project: [accessions]} from files named <PROJECT>_runs.txt."""
    projects: dict[str, list[str]] = {}
    for path in sorted(Path(folder).glob("*_runs.txt")):
        project = path.name[: -len("_runs.txt")]
        projects[project] = read_accessions(path)
    return projects


def ena_fetch(accessions: list[str]) -> list[dict]:
    """Query the ENA Portal API for a batch of run accessions (POST)."""
    query = " OR ".join(f"run_accession={a}" for a in accessions)
    payload = urllib.parse.urlencode(
        {
            "result": "read_run",
            "query": query,
            "fields": ",".join(ENA_FIELDS),
            "format": "json",
            "limit": 0,
        }
    ).encode()
    req = urllib.request.Request(
        ENA_SEARCH,
        data=payload,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        body = resp.read().decode("utf-8")
    return json.loads(body) if body.strip() else []


def _batches(items: list[str], size: int) -> Iterable[list[str]]:
    for i in range(0, len(items), size):
        yield items[i : i + size]


def fetch_metadata(
    accessions: list[str], cache_path: Path, fetcher: Fetcher = ena_fetch
) -> dict[str, dict]:
    cache: dict[str, dict] = {}
    if cache_path.exists():
        cache = json.loads(cache_path.read_text(encoding="utf-8"))
    missing = [a for a in accessions if a not in cache]
    for batch in _batches(missing, BATCH):
        for record in fetcher(batch):
            acc = record.get("run_accession")
            if acc:
                cache[acc] = record
    cache_path.parent.mkdir(parents=True, exist_ok=True)
    cache_path.write_text(json.dumps(cache, indent=1, sort_keys=True), encoding="utf-8")
    return cache


def _int(value) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _sum_bytes(value: str | None) -> int:
    if not value:
        return 0
    return sum(_int(v) for v in str(value).split(";"))


def build(
    folder: Path, out_dir: Path, fetcher: Fetcher = ena_fetch
) -> dict:
    """Build inventory.tsv and projects.tsv. Returns a summary with file hashes."""
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    projects = read_accession_lists(folder)
    all_accs = sorted({a for accs in projects.values() for a in accs})
    cache_path = out_dir / "ena_cache.json"
    meta = fetch_metadata(all_accs, cache_path, fetcher)

    inv_path = out_dir / "inventory.tsv"
    columns = ["project", "found_in_ena"] + ENA_FIELDS
    per_project: dict[str, dict] = defaultdict(
        lambda: {
            "accessions": 0,
            "found_in_ena": 0,
            "not_found": 0,
            "layouts": set(),
            "instruments": set(),
            "studies": set(),
            "reads": 0,
            "fastq_bytes": 0,
            "fastq_files": 0,
        }
    )
    with inv_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(columns)
        for project, accs in projects.items():
            stats = per_project[project]
            for acc in accs:
                rec = meta.get(acc)
                stats["accessions"] += 1
                if rec is None:
                    stats["not_found"] += 1
                    writer.writerow([project, "no", acc] + [""] * (len(ENA_FIELDS) - 1))
                    continue
                stats["found_in_ena"] += 1
                stats["layouts"].add(rec.get("library_layout") or "")
                stats["instruments"].add(rec.get("instrument_model") or "")
                stats["studies"].add(rec.get("study_accession") or "")
                stats["reads"] += _int(rec.get("read_count"))
                stats["fastq_bytes"] += _sum_bytes(rec.get("fastq_bytes"))
                stats["fastq_files"] += len([x for x in (rec.get("fastq_ftp") or "").split(";") if x])
                writer.writerow([project, "yes"] + [rec.get(f, "") for f in ENA_FIELDS])

    proj_path = out_dir / "projects.tsv"
    with proj_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh, delimiter="\t")
        writer.writerow(
            [
                "project",
                "accessions",
                "found_in_ena",
                "not_found",
                "studies",
                "library_layouts",
                "instrument_models",
                "n_instrument_models",
                "reads_total",
                "fastq_files",
                "fastq_gb",
                "n_studies",
                "n_library_layouts",
            ]
        )
        for project in projects:
            s = per_project[project]
            writer.writerow(
                [
                    project,
                    s["accessions"],
                    s["found_in_ena"],
                    s["not_found"],
                    ";".join(sorted(x for x in s["studies"] if x)),
                    ";".join(sorted(x for x in s["layouts"] if x)),
                    ";".join(sorted(x for x in s["instruments"] if x)),
                    len([x for x in s["instruments"] if x]),
                    s["reads"],
                    s["fastq_files"],
                    round(s["fastq_bytes"] / 1e9, 3),
                    len([x for x in s["studies"] if x]),
                    len([x for x in s["layouts"] if x]),
                ]
            )

    def sha(p: Path) -> str:
        return hashlib.sha256(p.read_bytes()).hexdigest()

    return {
        "projects": len(projects),
        "accessions": len(all_accs),
        "not_found": sum(s["not_found"] for s in per_project.values()),
        "files": {
            str(inv_path): sha(inv_path),
            str(proj_path): sha(proj_path),
            str(cache_path): sha(cache_path),
        },
    }
