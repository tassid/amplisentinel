# Ledger events

Every line of `.sentinel/ledger.jsonl` is one event. Each event has `ts`, `event`,
`actor` (`sentinel` or `human`), `user`, `host`, `git_commit`, `data`, `prev` and
`hash`. `actor: human` is only written by `sentinel decide` and by
`sentinel move ... -j`.

This file lists the events that the code writes today. Events of later stages are
documented here when their stage lands.

## Implemented

| Event | Written by | `data` fields |
|---|---|---|
| `workspace_created` | `sentinel new` | `name`, `version` |
| `init` | `sentinel init` | `version`, `workspace` |
| `runs_registered` | `sentinel init` (new runs only) | `runs` (list) |
| `state_change` | `sentinel move` | `run`, `from`, `to`, `justification` |
| `decision` | `sentinel decide` (`actor: human`) | `run`, `text` |
| `inventory_built` | `sentinel inventory` | `projects`, `accessions`, `not_found`, `files` (path to sha256) |
| `reconciliation_built` | `sentinel inventory --drive` | `runs`, `accessions`, `files`, `present_in_drive`, `equal`, `runs_without_drive_path`, `drive_listings` (per run: `drive_path`, `stdout` path and sha256, `version`), `output` (path and sha256 of `reconciliation.tsv`), `ena_cache` (path and sha256) |
| `tool_call` | any external tool, through `sentinel/ext/` | `command` (exact argv), `tool`, `version` (first line of `<tool> version`), `exit_code`, `stdout` and `stderr` (path and sha256) |
| `error` | an external tool that exits non-zero | `tool`, `command`, `exit_code`, `stderr` (path and sha256), plus `run` when the call belongs to a run. The text is in that file, byte for byte |
| `error` | `sentinel fetch` | `run`, `stage: fetch`, and either `files` (list of `accession`, `file`, `reason`, optional `log` path and sha256), `reason: espaço em disco` with the disk fields of `fetch_started`, or `reason: metadados do ENA` with `log` |
| `fetch_started` | `sentinel fetch` | `run`, `via` (`direct` or `fetchngs`), `source` (`auto`, `drive`, `ena`), `files`, `fastq_dir`, `resumed`, `bytes_total`, `bytes_remaining`, `disk_slack`, `disk_required`, `disk_free` |
| `file_verified` | `sentinel fetch` | `run`, `file`, `source` (`drive`, `ena`, `fetchngs`, `local`), `bytes`, `md5_expected` (ENA), `md5_obtained`, `equal` |
| `fetch_completed` | `sentinel fetch` | `run`, `manifest` (path and sha256 of `fetch_manifest.json`), `files`, `equal`, `result` (`MD5_OK` or `BLOCKED`) |
| `environment` | `sentinel run` | `run`, `ampliseq_release`, `nextflow_version`, `container_engine`, `container_engine_version`, `docker_version`, `colima_version`, `vm_config` (`name`, `status`, `vmType`, `rosetta`), `images` (list of `image`, `digest`), `conda_env` (`available`, `path`, `sha256` or `exit_code`/`stderr`), `samplesheet` and `params` (path and sha256) |
| `run_completed` | `sentinel run` | `run`, `result` (`PROCESSING` or `BLOCKED`), `manifest` (path and sha256 of `run_manifest.json`), `outdir`, `images`, `artifacts` (trace/report/timeline/nextflow_log paths and sha256), or `error_log` when `BLOCKED` |

## Notes

- Log files of external tools are in `.sentinel/logs/` as
  `<UTC timestamp>-<tool>.stdout` and `.stderr`. The ledger stores their sha256.
- `reconciliation.tsv` columns: `run`, `accession`, `ena_found`, `expected_file`,
  `ena_md5`, `ena_bytes`, `present_in_drive`, `drive_md5`, `equal`. Values are
  `yes`, `no`, or `not_checked` (the run has no `drive_path` in `runs.yaml`).
  `equal` is `yes` only when exactly one file with that name is on Drive and its
  MD5 equals the ENA MD5.

## fetch

- FASTQ files go to `<workspace>/runs/<RUN>/fastq/`. ENA downloads are written
  to `<file>.part` and renamed when the transfer ends; a later `fetch` continues
  the `.part` file with an HTTP Range request.
- `fetch_manifest.json` (in `runs/<RUN>/`) lists, per file: `file`, `accession`,
  `source`, `location`, `bytes_expected`, `bytes`, `md5_expected`, `md5_obtained`,
  `equal`; and the `problems` that led to `BLOCKED`.
- `source: local` means the file was already in `fastq/` and was checked as it
  is. Fetch never deletes or overwrites a file.

## run

- Inputs generated per run in `<workspace>/runs/<RUN>/ampliseq/`:
  - `samplesheet.tsv`: columns `sample`, `fastq_1`, `fastq_2` (omitted when all
    accessions are single-end), `run` (schema of nf-core/ampliseq 3.1.0).
  - `params.yaml`: `input`, `outdir`, `primer_fwd`, `primer_rev`, optional
    `metadata`, and `sequencing_type: illumina_se` for single-end runs. No
    truncation, trimming, or threshold parameters are added by Sentinel.
- Results go to `<workspace>/runs/<RUN>/results/` and Nextflow work files go to
  `<workspace>/runs/<RUN>/work/`. Trace, report, and timeline files go to
  `<workspace>/runs/<RUN>/ampliseq/pipeline_info/`.
- `run_manifest.json` (in `runs/<RUN>/`) records the command, generated inputs,
  image digests, produced artifacts, or `error_log` when `BLOCKED`.

## sentinel.yaml keys read by fetch and run

| Key | Meaning |
|---|---|
| `fetch.disk_slack` | free space needed = bytes still to download x this factor (default 1.2, must be >= 1) |
| `fetch.fetchngs_release` | nf-core/fetchngs release for `--via fetchngs`; required for that option |
| `run.ampliseq_release` | nf-core/ampliseq release passed via `-r` (default `3.1.0`) |
| `run.weblog_port` | local port for Nextflow `-with-weblog http://127.0.0.1:<port>/events` (default `8765`) |
| `container_engine` | `docker` (default) or `podman`, passed as `-profile` |
