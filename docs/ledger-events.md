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
| `error` | an external tool that exits non-zero | `tool`, `command`, `exit_code`, `stderr` (path and sha256). The text is in that file, byte for byte |

## Notes

- Log files of external tools are in `.sentinel/logs/` as
  `<UTC timestamp>-<tool>.stdout` and `.stderr`. The ledger stores their sha256.
- `reconciliation.tsv` columns: `run`, `accession`, `ena_found`, `expected_file`,
  `ena_md5`, `ena_bytes`, `present_in_drive`, `drive_md5`, `equal`. Values are
  `yes`, `no`, or `not_checked` (the run has no `drive_path` in `runs.yaml`).
  `equal` is `yes` only when exactly one file with that name is on Drive and its
  MD5 equals the ENA MD5.
