# Tools

Phase 1 runs locally on macOS (Apple Silicon). Everything is open source except the
public data services and the AI interface, listed at the end.

## Essential in phase 1

| Tool | Role in AmpliSentinel | License |
|---|---|---|
| QIIME 2 | Import, taxonomy, diversity; provenance in every .qza/.qzv | BSD-3-Clause |
| DADA2 | ASVs with one error model per sequencing run | LGPL-2 |
| cutadapt | Primer removal; % reads with primer | MIT |
| scikit-bio | PERMANOVA (R² by project vs by diagnosis) | BSD-3-Clause |
| QIIME 2 Provenance Replay | replay-supplement for each experiment | BSD-3-Clause |
| Nextflow | Workflow engine; -resume, trace, -with-weblog events | Apache-2.0 |
| nf-core/ampliseq | DADA2 + QIIME 2 pipeline, one sequencing run at a time | MIT |
| MultiQC | Quality report | GPL-3.0 |
| Colima | Docker Engine in a light VM on macOS, Rosetta for amd64 images | MIT (Lima, Docker CLI and Engine: Apache-2.0) |
| conda (conda-forge) | Python and ML environments | BSD-3-Clause |
| rclone | Google Drive read (drive.readonly) and write (drive.file), MD5 without download | MIT |
| DVC | Data versioned by MD5; dvc.lock per stage | Apache-2.0 |
| Git | Code, configs, rules | GPL-2.0 |
| MLflow | Experiment registry and comparison | Apache-2.0 |
| MCP | Read-only tools for the AI interface | MIT |
| pandas, scikit-learn, SHAP | ML stage: Random Forest, leave-one-project-out, SHAP | BSD-3-Clause / BSD-3-Clause / MIT |

## Optional or alternatives

| Tool | Role | License |
|---|---|---|
| Deblur | Alternative denoiser | BSD-3-Clause |
| decontam | Contaminants from negative controls | Artistic-2.0 |
| QIIME 2 View | Open .qzv in the browser | BSD-3-Clause |
| nf-core/fetchngs | Download FASTQ from SRA/ENA by accession | MIT |
| Snakemake | Alternative to Nextflow | MIT |
| Podman | Alternative to Colima (Rosetta support in flux) | Apache-2.0 |

## Not open source, kept on purpose

| Service | Role |
|---|---|
| SRA / ENA | Public raw data and official MD5 |
| SILVA | Taxonomic reference (free for academic use) |
| Google Drive | Working mirror of FASTQ and results |
| Claude | Conversational interface; reports data only (see CLAUDE.md) |
