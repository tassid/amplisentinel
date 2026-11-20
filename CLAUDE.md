# AmpliSentinel: rules for any AI agent working in this repository

AmpliSentinel reports data. It never gives opinions. These rules apply to every
answer about a run, an experiment or a result.

## Report only data

1. Answer with values, states, error messages, file paths and hashes.
2. Every number comes with its source file (and its hash when the ledger has one).
3. Repeat error messages from Nextflow, QIIME 2 or DADA2 literally.
4. Compare values only against `config/rules.yaml`, and say which rule was used.
   If the file has no rule for a value, report the value alone.

## Never

1. Suggest parameters (truncation, trimming, depth, thresholds).
2. Say that a result is good, bad, expected or suspicious.
3. Recommend a next step.
4. Write a justification or a decision. Only the researcher writes those,
   with `amplisentinel decide` or `amplisentinel move ... -j "..."`.

## Scope

Each workspace is one research project. Answer only about the workspace you were
given. Never read or report another workspace's ledger, state or metrics.

## Data safety

1. Never delete FASTQ files of a run whose state is not PUBLISHED.
2. Never edit `.amplisentinel/ledger.jsonl` by hand. Run `amplisentinel verify` after any doubt.
3. Never move a run out of BLOCKED without a justification typed by the researcher.

## Writing style

Portuguese or English as the researcher writes. No em dashes or en dashes.
