# AmpliSentinel

**Auditable experiment manager for multi-run amplicon processing.**
AmpliSentinel watches every step, records everything, and reports data. It never gives opinions.

Python package: `amplisentinel` · Command: `amplisentinel` · License: BSD-3-Clause

## Why

Processing ~100 GB of public 16S data from many studies on one laptop means running in parts:
download one sequencing run, process it, check it, publish the results, delete the raw files,
move to the next. AmpliSentinel makes that loop reproducible and auditable:

1. **Hash-chained ledger.** Every event is one JSON line carrying the hash of the line before it.
   Editing or deleting any past line is detected by `amplisentinel verify`.
2. **State machine per sequencing run.** `PENDING → FETCHING → MD5_OK → PROCESSING → MEASURED → PUBLISHED → CLEANED`,
   with `BLOCKED` reachable from the risky steps. Leaving `BLOCKED` requires a justification typed by the researcher.
3. **Official checksums.** Raw data identity is checked against the MD5 published by ENA.
4. **Data, not opinions.** Values are compared only against thresholds the researcher writes in
   `config/rules.yaml`. With an empty file, AmpliSentinel reports raw numbers only.
5. **Human decisions are marked as human.** The ledger separates `actor: amplisentinel` from `actor: human`.

## Architecture

```
            researcher ── amplisentinel CLI ──┐
                                         │
  OpenBot / Claude (MCP, read only) ─────┤
                                         ▼
                              ┌──────── manager ────────┐
                              │ state machine · ledger  │
                              └──┬───────┬──────┬────┬──┘
                                 │       │      │    │
                              fetch     run  measure publish/clean
                       (rclone / ENA) (Nextflow  (metrics)  (Drive, DVC)
                                      + nf-core/ampliseq
                                      + QIIME 2 in Colima)
```

## Status

| Component | State |
|---|---|
| Ledger (hash chain, verify) | working |
| Run state machine | working |
| Inventory (accession lists × ENA metadata) | working |
| Runs config (`runs.yaml`, unknown keys refused) | working |
| Drive reconciliation (ENA MD5 × rclone listing, no download) | working |
| External tool layer (`amplisentinel/ext/`, logs every call) | working |
| fetch (Drive via rclone, ENA HTTPS with resume, optional nf-core/fetchngs, MD5) | working |
| run (Nextflow + nf-core/ampliseq 3.1.0 per run) | working |
| measure (DADA2 stats, cutadapt, depth, MultiQC, rules) | working |
| publish / clean (Drive, DVC, MLflow) | working |
| merge (QIIME 2 merge, SILVA by MD5, replay-supplement) | working |
| between (PERMANOVA with scikit-bio) | working |
| ML bridge (export, LOPO folds, Random Forest, SHAP, MLflow) | working |
| MCP server (read only, 13 tools, serve-all, launchd) | working |
| report (deterministic static HTML report) | working |
| Browser dashboard (`amplisentinel ui`, 127.0.0.1, follows and runs commands) | working |
| Workspaces (one per research project) | working |
| CI and release packaging (`CITATION.cff`, `CHANGELOG.md`, workflow) | working |

## Workspaces: one per research project

Each research project (cervical cancer, rice, coral, soybean, coffee) is a separate
workspace with its own config, accession lists, ledger and state. Nothing is shared.

```
workspaces/cervical-cancer/
  amplisentinel.yaml            name, title
  config/runs.yaml         sequencing runs
  config/rules.yaml        thresholds written by the researcher
  data/accessions/         <STUDY>_runs.txt
  .amplisentinel/               ledger, state, inventory, metrics (not committed)
```

## Quick start

```bash
pip install -e ".[mcp]"
export AMPLISENTINEL_WORKSPACE=workspaces/cervical-cancer   # or: amplisentinel -w <path> ...
amplisentinel init            # registers runs from config/runs.yaml
amplisentinel inventory       # data/accessions/*_runs.txt × ENA → .amplisentinel/inventory/
amplisentinel inventory --drive   # also × Google Drive (rclone lsjson, nothing downloaded)
amplisentinel fetch P1        # FASTQ of one run → runs/P1/fastq/, MD5 checked against ENA
amplisentinel run P1          # nf-core/ampliseq 3.1.0 via Nextflow for one run
amplisentinel measure P1      # DADA2/cutadapt/depth/MultiQC metrics vs config/rules.yaml
amplisentinel publish P1      # upload artifacts to Drive, DVC, MLflow and verify MD5s
amplisentinel clean P1        # remove local FASTQ and work/ only after PUBLISHED check
amplisentinel report          # deterministic static HTML report in .amplisentinel/report.html
amplisentinel status
amplisentinel verify
amplisentinel show run P14    # JSON, the same data the MCP server returns
```

New project:

```bash
amplisentinel new ~/pesquisa/rice --name rice --title "Rice microbiome"
```

## Browser dashboard

```bash
pipx install git+https://github.com/tassid/amplisentinel.git   # or: pip install -e .
amplisentinel -w workspaces/cervical-cancer ui                 # opens http://127.0.0.1:8800/
```

- Runs on the same computer as the pipeline (macOS, Linux or Windows). Listens on
  127.0.0.1 only and refuses any other Host header.
- Pages: runs and states, one page per run (state, Nextflow process events,
  metrics with the rules in use, errors, ledger events), ledger with `verify`,
  commands started, and the HTML report.
- Buttons run the same `amplisentinel` commands as the terminal, one at a time, in a
  separate process. Refusals are identical. Each command is recorded in the
  ledger as `ui_command` (actor `human`) and `ui_command_finished` (exit code and
  sha256 of the output log under `.amplisentinel/ui/jobs/`).
- `move` and `decide` take the text typed by the researcher. `clean` needs a
  confirmation box.
- The Nextflow weblog receiver (same as `amplisentinel listen`) starts with the
  dashboard on `run.weblog_port`, so process events appear while `run` works.
  Use `--no-listen` when `amplisentinel listen` is already running, `--no-browser`
  to not open the browser, `--port` to change 8800.
- Every POST carries a random token created at start; a page from any other site
  cannot trigger a command.

## Conversational interface (OpenBot)

One read-only MCP server per project, one Bot per project. See [docs/openbot.md](docs/openbot.md).

```bash
amplisentinel -w workspaces/cervical-cancer mcp --http --port 8701
```

Moving a run and recording a decision:

```bash
amplisentinel move P1 FETCHING
amplisentinel move P1 BLOCKED
amplisentinel move P1 FETCHING -j "MD5 mismatch traced to an interrupted download; file fetched again"
amplisentinel decide P1 "<decision text typed by the researcher>"
```

## Data in this repository

`workspaces/cervical-cancer/data/accessions/` holds the public run accessions (SRA/ENA) of 10 cervicovaginal microbiome
studies, 1,042 runs in total. Raw FASTQ files are never committed.

## Open source stack

See [docs/tools.md](docs/tools.md) and [docs/release.md](docs/release.md).

## License

BSD-3-Clause. See [LICENSE](LICENSE).
