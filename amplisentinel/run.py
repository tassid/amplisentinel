"""Run worker: execute nf-core/ampliseq for one verified sequencing run.

`amplisentinel run <RUN>`:

1. Reads `runs.yaml` and `runs/<RUN>/fetch_manifest.json`. Refuses before
   changing state if the run is not in `MD5_OK` or `PROCESSING`, if `region`
   or `primers` are missing, or if any FASTQ listed in the manifest is absent
   or unverified.
2. Generates the nf-core/ampliseq 3.1.0 samplesheet (`samplesheet.tsv`) and
   parameter file (`params.yaml`) from `runs.yaml`. No scientific parameters
   (such as truncation or quality cutoffs) are suggested or filled in.
3. Transitions `MD5_OK -> PROCESSING` (or resumes `PROCESSING`).
4. Captures the execution environment through `amplisentinel/ext/` and appends an
   `environment` event to the ledger before running Nextflow: Nextflow version,
   ampliseq release, `colima version`, `docker version` (or `podman`), Colima
   VM settings (`vmType`, `rosetta`), container image digests, and the sha256 of
   `conda env export --no-builds`.
5. Executes Nextflow with `-r <release>`, `-profile <docker|podman>`, `-resume`,
   `-with-trace`, `-with-report`, `-with-timeline`, and
   `-with-weblog http://127.0.0.1:<port>/events`.
6. Success keeps the run in `PROCESSING` until `amplisentinel measure` (Stage 5).
   Any Nextflow or required-tool failure transitions `PROCESSING -> BLOCKED`
   with the literal stderr saved and referenced in the ledger.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

import yaml

from . import inventory as inv
from .common import log_mlflow_run
from .ext import Runner, ToolError, call, subprocess_runner
from .runs import ConfigError, RunConfig
from .workspace import Workspace

DEFAULT_AMPLISEQ_RELEASE = "3.1.0"
DEFAULT_WEBLOG_PORT = 8765
RUN_SETTINGS_KEYS = ("ampliseq_release", "weblog_port", "qiime2_image", "decontam_image")
ENGINES = ("docker", "podman")
SAMPLE_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9_]+$")
PAIR1_RE = re.compile(r"_1\.f(ast)?q\.gz$")
PAIR2_RE = re.compile(r"_2\.f(ast)?q\.gz$")


class RunRefused(Exception):
    """The run did not start. The run state did not change."""


@dataclass(frozen=True)
class RunSettings:
    ampliseq_release: str
    weblog_port: int
    container_engine: str


def settings(ws: Workspace, *, weblog_port_override: int | None = None) -> RunSettings:
    """Run settings from amplisentinel.yaml. Unknown keys in `run` are refused."""
    meta = ws.meta
    block = meta.get("run") or {}
    if not isinstance(block, dict):
        raise ConfigError("amplisentinel.yaml: 'run' precisa ser um mapa")
    unknown = sorted(str(k) for k in block if k not in RUN_SETTINGS_KEYS)
    if unknown:
        raise ConfigError(
            f"amplisentinel.yaml run: chave desconhecida {unknown}; aceitas: {', '.join(RUN_SETTINGS_KEYS)}"
        )
    release = block.get("ampliseq_release", DEFAULT_AMPLISEQ_RELEASE)
    if not isinstance(release, str) or not release.strip():
        raise ConfigError("amplisentinel.yaml run.ampliseq_release: esperado texto")
    port = weblog_port_override if weblog_port_override is not None else block.get("weblog_port", DEFAULT_WEBLOG_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError("amplisentinel.yaml run.weblog_port: esperado inteiro entre 1 e 65535")
    engine = meta.get("container_engine", "docker")
    if engine not in ENGINES:
        raise ConfigError(f"amplisentinel.yaml container_engine: aceitos {', '.join(ENGINES)}")
    return RunSettings(release.strip(), port, engine)


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    return _sha256_bytes(path.read_bytes())


def _write_text_atomic(path: Path, text: str) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)
    return _sha256_file(path)


def _write_json_atomic(path: Path, data: dict) -> str:
    return _write_text_atomic(path, json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True))


def _pick_pair(acc: str, paths: list[Path]) -> tuple[Path, Path | None]:
    if len(paths) == 1:
        return paths[0], None
    if len(paths) == 2:
        p1 = [p for p in paths if PAIR1_RE.search(p.name)]
        p2 = [p for p in paths if PAIR2_RE.search(p.name)]
        if len(p1) == 1 and len(p2) == 1:
            return p1[0], p2[0]
        return paths[0], paths[1]
    p1 = [p for p in paths if PAIR1_RE.search(p.name)]
    p2 = [p for p in paths if PAIR2_RE.search(p.name)]
    if len(p1) == 1 and len(p2) == 1:
        return p1[0], p2[0]
    raise RunRefused(f"accession {acc}: {len(paths)} arquivos FASTQ sem par _1/_2 único")


def _load_samplesheet_rows(ws: Workspace, run: str, rc: RunConfig) -> tuple[list[dict[str, str]], bool]:
    manifest_path = ws.root / "runs" / run / "fetch_manifest.json"
    if not manifest_path.exists():
        raise RunRefused(f"corrida {run}: falta {manifest_path}; execute `amplisentinel fetch {run}` antes")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RunRefused(f"corrida {run}: fetch_manifest.json inválido ({exc})") from None

    entries = manifest.get("files") or []
    if not isinstance(entries, list) or not entries:
        raise RunRefused(f"corrida {run}: fetch_manifest.json sem arquivos")

    fastq_dir = ws.root / "runs" / run / "fastq"
    by_acc: dict[str, list[Path]] = {}
    for entry in entries:
        acc = entry.get("accession")
        name = entry.get("file")
        if not acc or not name:
            raise RunRefused(f"corrida {run}: entrada incompleta em fetch_manifest.json")
        if entry.get("equal") is not True:
            raise RunRefused(f"corrida {run}: arquivo {name} não está verificado (equal != true)")
        fpath = (fastq_dir / name).resolve()
        if not fpath.exists():
            raise RunRefused(f"corrida {run}: FASTQ ausente no disco: {fpath}")
        by_acc.setdefault(acc, []).append(fpath)

    acc_order: list[str] = []
    if rc.accessions and (ws.root / rc.accessions).exists():
        acc_order = [a for a in inv.read_accessions(ws.root / rc.accessions) if a in by_acc]
    for acc in by_acc:
        if acc not in acc_order:
            acc_order.append(acc)

    rows: list[dict[str, str]] = []
    layouts: set[bool] = set()
    for acc in acc_order:
        if not SAMPLE_RE.match(acc):
            raise RunRefused(
                f"accession {acc} não atende ao padrão de sample do ampliseq (^[a-zA-Z][a-zA-Z0-9_]+$)"
            )
        r1, r2 = _pick_pair(acc, by_acc[acc])
        is_paired = r2 is not None
        layouts.add(is_paired)
        row = {"sample": acc, "fastq_1": str(r1), "run": run}
        if r2 is not None:
            row["fastq_2"] = str(r2)
        rows.append(row)

    if len(layouts) != 1:
        raise RunRefused(f"corrida {run}: mistura arquivos single-end e paired-end no mesmo samplesheet")
    return rows, next(iter(layouts))


def render_samplesheet(rows: list[dict[str, str]], paired: bool) -> str:
    """Format the nf-core/ampliseq 3.1.0 TSV samplesheet."""
    cols = ["sample", "fastq_1", "fastq_2", "run"] if paired else ["sample", "fastq_1", "run"]
    lines = ["\t".join(cols)]
    for row in rows:
        lines.append("\t".join(row[c] for c in cols))
    return "\n".join(lines) + "\n"


def render_params(
    samplesheet_path: Path,
    outdir: Path,
    rc: RunConfig,
    paired: bool,
    metadata_path: Path | None,
) -> dict[str, str]:
    """Parameters written to params.yaml for nf-core/ampliseq 3.1.0."""
    data: dict[str, str] = {
        "input": str(samplesheet_path),
        "outdir": str(outdir),
        "primer_fwd": rc.primers.forward or "",
        "primer_rev": rc.primers.reverse or "",
    }
    if metadata_path is not None:
        data["metadata"] = str(metadata_path)
    if not paired:
        data["sequencing_type"] = "illumina_se"
    return data


def _first_line(data: bytes) -> str | None:
    lines = [line.strip() for line in data.decode("utf-8", errors="replace").splitlines() if line.strip()]
    return lines[0] if lines else None


def parse_colima_vm(stdout: bytes) -> dict | None:
    """Extract vmType and rosetta settings from `colima list --json` output."""
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return None
    items: list[dict] = []
    try:
        parsed = json.loads(text)
        if isinstance(parsed, list):
            items = [x for x in parsed if isinstance(x, dict)]
        elif isinstance(parsed, dict):
            items = [parsed]
    except json.JSONDecodeError:
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                items.append(obj)
    if not items:
        return None

    chosen = next((i for i in items if str(i.get("status", "")).lower() == "running"), None)
    if chosen is None:
        chosen = next((i for i in items if i.get("name") == "default"), items[0])
    cfg = chosen.get("config") if isinstance(chosen.get("config"), dict) else {}
    vm_type = chosen.get("vmType") or cfg.get("vmType")

    rosetta: bool | None = None
    vm_opts = cfg.get("vmOpts") if isinstance(cfg.get("vmOpts"), dict) else {}
    vz = vm_opts.get("vz") if isinstance(vm_opts.get("vz"), dict) else {}
    vz_rosetta = vz.get("rosetta")
    if isinstance(vz_rosetta, dict) and "enabled" in vz_rosetta:
        rosetta = bool(vz_rosetta["enabled"])
    elif isinstance(vz_rosetta, bool):
        rosetta = vz_rosetta
    elif isinstance(cfg.get("rosetta"), bool):
        rosetta = cfg["rosetta"]
    elif isinstance(chosen.get("rosetta"), bool):
        rosetta = chosen["rosetta"]

    return {
        "name": chosen.get("name"),
        "status": chosen.get("status"),
        "vmType": vm_type,
        "rosetta": rosetta,
    }


def parse_inspect_images(stdout: bytes) -> list[str]:
    """Return unique container image URIs declared by `nextflow inspect -format json`."""
    text = stdout.decode("utf-8", errors="replace").strip()
    if not text:
        return []
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return []
    processes = data.get("processes") if isinstance(data, dict) else None
    if not isinstance(processes, list):
        return []
    seen: list[str] = []
    for proc in processes:
        if not isinstance(proc, dict):
            continue
        image = proc.get("container")
        if isinstance(image, str) and image.strip() and image.strip() not in seen:
            seen.append(image.strip())
    return seen


def parse_image_digests(stdout: bytes) -> dict[str, str]:
    """Parse `<Repository>\\t<Tag>\\t<Digest>\\t<ID>` lines into {image_ref: digest}."""
    digests: dict[str, str] = {}
    for raw in stdout.decode("utf-8", errors="replace").splitlines():
        parts = raw.split("\t")
        if len(parts) < 3:
            continue
        repo, tag, digest = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if not repo or repo == "<none>" or not digest or digest == "<none>":
            continue
        key = f"{repo}:{tag}" if tag and tag != "<none>" else repo
        digests[key] = digest
        if repo not in digests:
            digests[repo] = digest
    return digests


def resolve_images(inspected: list[str], local_digests: dict[str, str]) -> list[dict]:
    """Pair pipeline container references with their local digests."""
    if not inspected:
        return [{"image": img, "digest": dig} for img, dig in sorted(local_digests.items())]
    resolved: list[dict] = []
    for image in inspected:
        digest: str | None = None
        if "@sha256:" in image:
            digest = "sha256:" + image.split("@sha256:", 1)[1]
        elif image in local_digests:
            digest = local_digests[image]
        else:
            no_reg = image.split("/", 1)[1] if "/" in image and "." in image.split("/", 1)[0] else image
            digest = local_digests.get(no_reg)
        resolved.append({"image": image, "digest": digest})
    return resolved


def _local_image_digests(
    ws: Workspace, run: str, engine: str, runner: Runner
) -> dict[str, str]:
    rec = call(
        ws,
        [engine, "images", "--digests", "--no-trunc", "--format", "{{.Repository}}\t{{.Tag}}\t{{.Digest}}\t{{.ID}}"],
        version_argv=[engine, "--version"],
        runner=runner,
        context={"run": run},
        check=False,
    )
    if rec.returncode != 0:
        return {}
    return parse_image_digests(rec.stdout)


def collect_environment(
    ws: Workspace,
    run: str,
    cfg: RunSettings,
    params_path: Path,
    samplesheet_info: dict,
    params_info: dict,
    runner: Runner,
    environ: Mapping[str, str],
) -> dict:
    """Probe versions, VM settings, image digests, and conda env hash; append to ledger."""
    ctx = {"run": run}
    nf_rec = call(
        ws,
        ["nextflow", "-version"],
        version_argv=["nextflow", "-version"],
        runner=runner,
        context=ctx,
    )
    nf_version = nf_rec.version or _first_line(nf_rec.stdout)

    engine = cfg.container_engine
    eng_rec = call(
        ws,
        [engine, "version"],
        version_argv=[engine, "--version"],
        runner=runner,
        context=ctx,
    )
    eng_version = eng_rec.version or _first_line(eng_rec.stdout)

    colima_ver_rec = call(
        ws,
        ["colima", "version"],
        version_argv=["colima", "version"],
        runner=runner,
        context=ctx,
        check=False,
    )
    colima_version = (
        (colima_ver_rec.version or _first_line(colima_ver_rec.stdout))
        if colima_ver_rec.returncode == 0
        else None
    )
    colima_list_rec = call(
        ws,
        ["colima", "list", "--json"],
        version_argv=["colima", "version"],
        runner=runner,
        context=ctx,
        check=False,
    )
    vm_config = parse_colima_vm(colima_list_rec.stdout) if colima_list_rec.returncode == 0 else None

    inspect_rec = call(
        ws,
        [
            "nextflow",
            "inspect",
            "nf-core/ampliseq",
            "-r",
            cfg.ampliseq_release,
            "-profile",
            engine,
            "-params-file",
            str(params_path),
            "-format",
            "json",
        ],
        version_argv=["nextflow", "-version"],
        runner=runner,
        context=ctx,
        check=False,
    )
    inspected = parse_inspect_images(inspect_rec.stdout) if inspect_rec.returncode == 0 else []
    local_digests = _local_image_digests(ws, run, engine, runner)
    images = resolve_images(inspected, local_digests)

    conda_argv = ["conda", "env", "export", "--no-builds"]
    prefix = environ.get("CONDA_PREFIX")
    if prefix:
        conda_argv.extend(["-p", prefix])
    conda_rec = call(
        ws,
        conda_argv,
        version_argv=["conda", "--version"],
        runner=runner,
        context=ctx,
        check=False,
    )
    if conda_rec.returncode == 0:
        conda_info: dict = {
            "available": True,
            "path": conda_rec.stdout_path,
            "sha256": conda_rec.stdout_sha256,
            "prefix": prefix,
        }
    else:
        conda_info = {
            "available": False,
            "exit_code": conda_rec.returncode,
            "stderr": {"path": conda_rec.stderr_path, "sha256": conda_rec.stderr_sha256},
        }

    env_data = {
        "run": run,
        "ampliseq_release": cfg.ampliseq_release,
        "nextflow_version": nf_version,
        "container_engine": engine,
        "container_engine_version": eng_version,
        "docker_version": eng_version if engine == "docker" else None,
        "colima_version": colima_version,
        "vm_config": vm_config,
        "images": images,
        "conda_env": conda_info,
        "samplesheet": samplesheet_info,
        "params": params_info,
    }
    ws.ledger.append("environment", env_data)
    return env_data


def _file_info(path: Path) -> dict | None:
    if not path.exists():
        return None
    return {"path": str(path), "sha256": _sha256_file(path)}


def run_ampliseq(
    ws: Workspace,
    run: str,
    *,
    weblog_port: int | None = None,
    runner: Runner = subprocess_runner,
    environ: Mapping[str, str] | None = None,
) -> dict:
    """Generate ampliseq inputs, record environment, and run Nextflow for one run."""
    runs = ws.runs()
    if run not in runs:
        raise RunRefused(f"corrida desconhecida neste workspace: {run}")
    rc = runs[run]
    cfg = settings(ws, weblog_port_override=weblog_port)

    current = ws.state.get(run)
    if current == "BLOCKED":
        raise RunRefused(f"{run} está em BLOCKED; sair de BLOCKED exige `amplisentinel move {run} PROCESSING -j`")
    if current not in ("MD5_OK", "PROCESSING"):
        raise RunRefused(f"{run} está em {current}; run só roda em MD5_OK ou PROCESSING")

    if not rc.region:
        raise RunRefused(f"corrida {run}: falta 'region' em config/runs.yaml")
    if not rc.primers.forward or not rc.primers.reverse:
        raise RunRefused(f"corrida {run}: faltam 'primers.forward' e 'primers.reverse' em config/runs.yaml")

    metadata_path: Path | None = None
    if rc.metadata:
        metadata_path = (ws.root / rc.metadata).resolve()
        if not metadata_path.exists():
            raise RunRefused(f"corrida {run}: arquivo metadata não encontrado: {metadata_path}")

    rows, paired = _load_samplesheet_rows(ws, run, rc)

    run_dir = ws.root / "runs" / run
    ampliseq_dir = run_dir / "ampliseq"
    info_dir = ampliseq_dir / "pipeline_info"
    outdir = run_dir / "results"
    work_dir = run_dir / "work"
    info_dir.mkdir(parents=True, exist_ok=True)
    outdir.mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    samplesheet_path = ampliseq_dir / "samplesheet.tsv"
    ss_sha = _write_text_atomic(samplesheet_path, render_samplesheet(rows, paired))
    samplesheet_info = {"path": str(samplesheet_path), "sha256": ss_sha}

    params_path = ampliseq_dir / "params.yaml"
    params_obj = render_params(samplesheet_path, outdir, rc, paired, metadata_path)
    params_sha = _write_text_atomic(params_path, yaml.safe_dump(params_obj, sort_keys=False))
    params_info = {"path": str(params_path), "sha256": params_sha}

    if current == "MD5_OK":
        ws.state.move(run, "PROCESSING")

    env_map = os.environ if environ is None else environ
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    nf_log = ampliseq_dir / f"nextflow-{stamp}.log"
    trace_path = info_dir / f"trace-{stamp}.txt"
    report_path = info_dir / f"report-{stamp}.html"
    timeline_path = info_dir / f"timeline-{stamp}.html"

    manifest_path = run_dir / "run_manifest.json"

    try:
        env_data = collect_environment(
            ws,
            run,
            cfg,
            params_path,
            samplesheet_info,
            params_info,
            runner,
            env_map,
        )
        argv = [
            "nextflow",
            "-log",
            str(nf_log),
            "run",
            "nf-core/ampliseq",
            "-r",
            cfg.ampliseq_release,
            "-profile",
            cfg.container_engine,
            "-params-file",
            str(params_path),
            "-work-dir",
            str(work_dir),
            "-resume",
            "-with-trace",
            str(trace_path),
            "-with-report",
            str(report_path),
            "-with-timeline",
            str(timeline_path),
            "-with-weblog",
            f"http://127.0.0.1:{cfg.weblog_port}/events",
        ]
        call(ws, argv, version_argv=["nextflow", "-version"], runner=runner, context={"run": run})
    except ToolError as exc:
        manifest = {
            "run": run,
            "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "ampliseq_release": cfg.ampliseq_release,
            "result": "BLOCKED",
            "samplesheet": samplesheet_info,
            "params": params_info,
            "outdir": str(outdir),
            "error_log": exc.log,
        }
        m_sha = _write_json_atomic(manifest_path, manifest)
        ws.ledger.append(
            "run_completed",
            {
                "run": run,
                "result": "BLOCKED",
                "manifest": {"path": str(manifest_path), "sha256": m_sha},
                "error_log": exc.log,
            },
        )
        ws.state.move(run, "BLOCKED")
        return {
            "run": run,
            "state": "BLOCKED",
            "samples": len(rows),
            "samplesheet": samplesheet_info,
            "params": params_info,
            "manifest": {"path": str(manifest_path), "sha256": m_sha},
            "error_log": exc.log,
            "error": str(exc),
        }

    post_digests = _local_image_digests(ws, run, cfg.container_engine, runner)
    inspected_names = [img["image"] for img in env_data.get("images", [])]
    final_images = resolve_images(inspected_names, post_digests) if post_digests else env_data.get("images", [])

    artifacts = {
        "nextflow_log": _file_info(nf_log),
        "trace": _file_info(trace_path),
        "report": _file_info(report_path),
        "timeline": _file_info(timeline_path),
    }
    manifest = {
        "run": run,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "ampliseq_release": cfg.ampliseq_release,
        "container_engine": cfg.container_engine,
        "result": "PROCESSING",
        "command": argv,
        "samplesheet": samplesheet_info,
        "params": params_info,
        "outdir": str(outdir),
        "images": final_images,
        "artifacts": {k: v for k, v in artifacts.items() if v is not None},
    }
    mlflow_info = log_mlflow_run(
        ws,
        run_name=f"{run}-dada2-{stamp}",
        params={
            "denoiser": "dada2",
            "ampliseq_release": cfg.ampliseq_release,
            "container_engine": cfg.container_engine,
            "region": rc.region or "",
            "primer_fwd": rc.primers.forward or "",
            "primer_rev": rc.primers.reverse or "",
        },
        metrics={"samples": float(len(rows))},
        tags={"run": run, "denoiser": "dada2"},
        artifacts=[samplesheet_path, params_path],
    )
    if mlflow_info is not None:
        manifest["mlflow"] = mlflow_info
    m_sha = _write_json_atomic(manifest_path, manifest)
    ws.ledger.append(
        "run_completed",
        {
            "run": run,
            "result": "PROCESSING",
            "manifest": {"path": str(manifest_path), "sha256": m_sha},
            "outdir": str(outdir),
            "images": final_images,
            "artifacts": manifest["artifacts"],
        },
    )
    return {
        "run": run,
        "state": "PROCESSING",
        "samples": len(rows),
        "samplesheet": samplesheet_info,
        "params": params_info,
        "manifest": {"path": str(manifest_path), "sha256": m_sha},
        "error_log": None,
    }
