# Changelog

All notable changes to `amplisentinel` are documented in this file.

The format follows Keep a Changelog and uses Semantic Versioning.

## [Unreleased]

### Changed
- Project renamed to AmpliSentinel everywhere: Python package `amplisentinel`, command `amplisentinel`, variable `AMPLISENTINEL_WORKSPACE`, workspace file `amplisentinel.yaml`, data folder `.amplisentinel/`, `~/.amplisentinel/workspaces.yaml`, ledger actor `amplisentinel`, repository `tassid/amplisentinel`.

### Added
- `amplisentinel ui`: local browser dashboard on 127.0.0.1 (runs, states, Nextflow process events, metrics with rules, errors, ledger, report) with buttons that run the same CLI commands one at a time, recorded as `ui_command` and `ui_command_finished`. Starts the weblog receiver with it.

### Fixed
- Ledger appends now hold a cross-process file lock, so concurrent writers (`run` with `listen` or `ui`) cannot fork the hash chain.
- On Windows the weblog receiver and the dashboard refuse a port already in use instead of binding it silently.

## [0.2.0] - 2026-11-19

### Added
- Stage 1: validated `config/runs.yaml` schema, `amplisentinel/ext/` logged tool runner, Drive reconciliation (`amplisentinel inventory --drive`), and study layout counts in `projects.tsv`.
- Stage 2: `amplisentinel fetch <RUN>` supporting Google Drive (`rclone`), ENA HTTPS with HTTP Range resume, optional `nf-core/fetchngs`, disk slack checks, and MD5 verification against ENA.
- Stage 3 and 3b: `amplisentinel run <RUN>` for `nf-core/ampliseq` `3.1.0` (samplesheet, params, environment/image digest/conda hash logging before execution) and optional `--denoiser deblur` isolated QIIME 2 experiment.
- Stage 4: `amplisentinel listen` local `127.0.0.1` HTTP weblog receiver for Nextflow process events.
- Stage 5 and 5b: `amplisentinel measure <RUN>` with `pandas` over DADA2, cutadapt, ASV lengths, taxonomy, depth, and MultiQC against `config/rules.yaml`; optional `amplisentinel decontam <RUN>` when `negative_controls` are declared.
- Stage 6: `amplisentinel publish <RUN>` (`rclone`, DVC, MLflow, remote MD5 verification) and `amplisentinel clean <RUN>` restricted to `PUBLISHED` after remote hash re-verification.
- Stage 7: `amplisentinel merge` enforcing `PUBLISHED`/`CLEANED` runs, researcher decision on multi-region conflicts, SILVA MD5 verification, and QIIME 2 `replay-supplement`.
- Stage 8: `amplisentinel between` PERMANOVA (study and diagnosis) with `scikit-bio` and fixed seed.
- Stage 9: `environment.yml`, `amplisentinel export` (recording `.qza` UUID), deterministic LOPO `amplisentinel folds`, and `amplisentinel train` (Random Forest, t-SNE, SHAP, MLflow).
- Stage 10: five new read-only MCP tools (`reconciliation`, `fetch_manifest`, `process_events`, `between_metrics`, `folds`), `amplisentinel serve-all`, and `amplisentinel launchd` plist templates.
- Stage 11: `amplisentinel report` deterministic static HTML report with literal human decisions and `.qzv` hashes.
- Stage 12: GitHub Actions CI for Linux and macOS, `CITATION.cff`, `.zenodo.json`, `CHANGELOG.md`, and release documentation.

## [0.1.0] - 2026-11-19

### Added
- Hash-chained JSONL ledger (`amplisentinel verify`), per-run state machine (`amplisentinel move`, `amplisentinel decide`), ENA inventory, per-project workspaces, and read-only MCP server (`8` initial tools).
