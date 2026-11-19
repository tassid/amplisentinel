# Sentinel

**Auditable experiment manager for multi-run amplicon processing.**
Sentinel watches every step, records everything, and reports data. It never gives opinions.

Python package: `amplisentinel` · Command: `sentinel` · License: BSD-3-Clause

## Why

Processing ~100 GB of public 16S data from many studies on one laptop means running in parts:
download one sequencing run, process it, check it, publish the results, delete the raw files,
move to the next. Sentinel makes that loop reproducible and auditable:

1. **Hash-chained ledger.** Every event is one JSON line carrying the hash of the line before it.
   Editing or deleting any past line is detected by `sentinel verify`.
2. **State machine per sequencing run.** `PENDING → FETCHING → MD5_OK → PROCESSING → MEASURED → PUBLISHED → CLEANED`,
   with `BLOCKED` reachable from the risky steps. Leaving `BLOCKED` requires a justification typed by the researcher.
3. **Official checksums.** Raw data identity is checked against the MD5 published by ENA.
4. **Data, not opinions.** Values are compared only against thresholds the researcher writes in
   `config/rules.yaml`. With an empty file, Sentinel reports raw numbers only.
5. **Human decisions are marked as human.** The ledger separates `actor: sentinel` from `actor: human`.

## Architecture

```
            researcher ── sentinel CLI ──┐
                                         │
  Claude (MCP, read only, no opinions) ──┤
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
| fetch (rclone, ENA, MD5) | next |
| run (Nextflow + ampliseq per run) | planned |
| measure (DADA2 stats, cutadapt, depth, PERMANOVA) | planned |
| publish / clean (Drive, DVC, MLflow) | planned |
| MCP server (read only) | planned |

## Quick start

```bash
pip install -e .
sentinel init            # creates .sentinel/ and registers runs from config/runs.yaml
sentinel inventory       # data/accessions/*_runs.txt × ENA → .sentinel/inventory/
sentinel status
sentinel verify
```

Moving a run and recording a decision:

```bash
sentinel move P1 FETCHING
sentinel move P1 BLOCKED
sentinel move P1 FETCHING -j "MD5 mismatch traced to an interrupted download; file fetched again"
sentinel decide P1 "Truncation 240/200 based on the quality profile in demux.qzv"
```

## Data in this repository

`data/accessions/` holds the public run accessions (SRA/ENA) of 10 cervicovaginal microbiome
studies, 1,042 runs in total. Raw FASTQ files are never committed.

## Open source stack

See [docs/tools.md](docs/tools.md).

## License

BSD-3-Clause. See [LICENSE](LICENSE).
