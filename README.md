<p align="center"><img src="docs/assets/amplisentinel-mascote.svg" alt="AmpliSentinel mascot: a capybara holding a shield with a DNA double helix" width="200"></p>

# AmpliSentinel

**Auditable experiment manager for multi-run amplicon processing.**
AmpliSentinel watches every step, records everything, and reports data. It never gives opinions.

Python package: `amplisentinel` · Command: `amplisentinel` · License: BSD-3-Clause

**Install:** [macOS](#macos-apple-silicon-or-intel) · [Linux](#linux) · [Windows](#windows) · [Commands](#commands) · [Dashboard](#browser-dashboard)

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
  amplisentinel.yaml       name, title, tool versions and image digests
  config/runs.yaml         sequencing runs
  config/rules.yaml        thresholds written by the researcher
  data/accessions/         <STUDY>_runs.txt
  .amplisentinel/          ledger, state, inventory, metrics (not committed)
```

## Installation

AmpliSentinel runs on the computer that processes the data. The Python package is
tested by CI on macOS and Linux (Python 3.10, 3.12, 3.13). Nextflow, nf-core/ampliseq
and the containers have not yet been validated end to end on a real run.

### macOS (Apple Silicon or Intel)

macOS 13 or later. Every step ends with a check; continue only when it prints what
is described.

**1. Homebrew** (package manager). Skip if `brew --version` already works.

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

On Apple Silicon, run the two `echo ... >> ~/.zprofile` and `eval ...` lines that the
installer prints at the end, then check: `brew --version`.

**2. Tools**

```bash
brew install git rclone nextflow colima docker
brew install --cask miniforge
conda init "$(basename "$SHELL")"
```

Close and reopen the Terminal, then check:

```bash
nextflow -version && rclone version && conda --version
```

| Tool | Used for |
|---|---|
| Nextflow (installs Java with it) | runs nf-core/ampliseq |
| Colima + Docker CLI | runs the pipeline containers in a small Linux VM |
| rclone | reads and writes Google Drive, gets MD5 without downloading |
| Miniforge (conda) | Python environment with pinned versions |

**3. Start Colima** (the container engine). CPU, memory and disk below are examples;
use what your Mac has to spare.

```bash
# Apple Silicon: Rosetta lets the VM run amd64 images (QIIME 2 and others)
colima start --cpu 4 --memory 8 --disk 100 --vm-type vz --vz-rosetta

# Intel
colima start --cpu 4 --memory 8 --disk 100
```

Check: `docker run --rm hello-world` prints "Hello from Docker!".
Colima stops when the Mac restarts. Run `colima start` again (it keeps the settings),
or start it at login with `brew services start colima`.

**4. AmpliSentinel**

Keep the clone inside your home folder: Colima shares it with the containers.

```bash
cd ~
git clone https://github.com/tassid/amplisentinel.git
cd amplisentinel
conda env create -f environment.yml
conda activate amplisentinel
pip install -e ".[mcp]"
```

Check: `amplisentinel --version` prints the version.
Every new Terminal needs `conda activate amplisentinel` before using the command.

**5. Google Drive** (only for `inventory --drive`, fetching from Drive, and `publish`)

```bash
rclone config
```

- Create a remote of type `drive` with scope `drive.readonly` (read only), for
  example named `gdrive`. Sign in when the browser opens. Then each run in
  `config/runs.yaml` can point to its folder: `drive_path: "gdrive:<folder>/P1"`.
- For `publish`, create a second remote named exactly `gdrive_rw` with scope
  `drive.file` (writes only files that rclone created).

Check: `rclone lsd gdrive:` lists your top folders.

**6. First run** (only reads ENA, downloads nothing)

```bash
cd ~/amplisentinel
export AMPLISENTINEL_WORKSPACE=workspaces/cervical-cancer
amplisentinel init        # registers the runs of config/runs.yaml
amplisentinel inventory   # accession lists x ENA metadata
amplisentinel ui          # opens the dashboard at http://127.0.0.1:8800/
```

Before `fetch` and `run` work, the researcher fills in:

| Where | What | Needed by |
|---|---|---|
| `config/runs.yaml` | `region`, `primers.forward`, `primers.reverse` of each run | `run` |
| `config/runs.yaml` | `drive_path` of each run | `inventory --drive`, `fetch` from Drive |
| `config/rules.yaml` | thresholds (optional; empty means raw numbers only) | `measure` |
| `amplisentinel.yaml` | `run.qiime2_image` pinned by digest (`...@sha256:...`) | `run --denoiser deblur`, `merge` |
| `amplisentinel.yaml` | `merge.silva_classifier` and `merge.silva_md5` | `merge` |

Every command refuses, without changing any state, while something it needs is missing,
and prints what is missing.

**Update**

```bash
cd ~/amplisentinel && git pull && conda activate amplisentinel && pip install -e ".[mcp]"
```

**Common problems**

| Message | Cause and fix |
|---|---|
| `command not found: amplisentinel` | environment not active: `conda activate amplisentinel` |
| `Cannot connect to the Docker daemon` | Colima stopped: `colima start` |
| `command not found: brew` (Apple Silicon) | step 1 PATH lines were not run; run them and reopen the Terminal |
| `receptor weblog não iniciado` | port 8765 in use; set another in `amplisentinel.yaml` (`run.weblog_port`) |
| dashboard port in use | `amplisentinel ui --port 8801` |

### Linux

Same steps as macOS from step 4, with the tools installed by the distribution:
[Docker Engine](https://docs.docker.com/engine/install/) (add your user to the
`docker` group), Java 17 or later, [Nextflow](https://www.nextflow.io/docs/latest/install.html),
[rclone](https://rclone.org/install/) and [Miniforge](https://github.com/conda-forge/miniforge).
No Colima needed.

### Windows

Nextflow does not run natively on Windows. Use WSL2 with Ubuntu and Docker Desktop
(WSL integration on), then follow the Linux steps inside Ubuntu. Not tested yet.

## Commands

```bash
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
amplisentinel -w workspaces/cervical-cancer ui      # opens http://127.0.0.1:8800/
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
