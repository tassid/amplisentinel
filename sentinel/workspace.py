"""A workspace is one research project (for example cervical-cancer, rice, coral).

Layout:

  <workspace>/
    sentinel.yaml            name, title, description
    config/runs.yaml         sequencing runs of this project
    config/rules.yaml        thresholds written by the researcher
    data/accessions/         <STUDY>_runs.txt accession lists
    .sentinel/ledger.jsonl   hash-chained ledger (never edited by hand)
    .sentinel/state.json     state of each run
    .sentinel/inventory/     inventory tables and ENA cache
    .sentinel/metrics/       <RUN>.json metrics written by `measure`

Everything Sentinel reads or writes for a project stays inside its workspace.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .ledger import Ledger
from .runs import RunConfig, load_runs
from .state import RunState

TEMPLATE_RUNS = """# One entry per sequencing run. DADA2 learns one error model per entry.
runs: {}
"""

TEMPLATE_RULES = """# Thresholds written by the researcher. Empty means raw numbers only.
"""


class WorkspaceError(Exception):
    pass


@dataclass
class Workspace:
    root: Path

    @classmethod
    def open(cls, path: str | Path) -> Workspace:
        root = Path(path).expanduser().resolve()
        if not (root / "sentinel.yaml").exists():
            raise WorkspaceError(f"não é um workspace do Sentinel (falta sentinel.yaml): {root}")
        return cls(root)

    @classmethod
    def create(cls, path: str | Path, name: str, title: str | None = None) -> Workspace:
        root = Path(path).expanduser().resolve()
        if (root / "sentinel.yaml").exists():
            raise WorkspaceError(f"já existe um workspace em {root}")
        (root / "config").mkdir(parents=True, exist_ok=True)
        (root / "data" / "accessions").mkdir(parents=True, exist_ok=True)
        (root / "sentinel.yaml").write_text(
            yaml.safe_dump({"name": name, "title": title or name, "description": ""}, sort_keys=False),
            encoding="utf-8",
        )
        if not (root / "config" / "runs.yaml").exists():
            (root / "config" / "runs.yaml").write_text(TEMPLATE_RUNS, encoding="utf-8")
        if not (root / "config" / "rules.yaml").exists():
            (root / "config" / "rules.yaml").write_text(TEMPLATE_RULES, encoding="utf-8")
        return cls(root)

    @property
    def meta(self) -> dict:
        return yaml.safe_load((self.root / "sentinel.yaml").read_text(encoding="utf-8")) or {}

    @property
    def name(self) -> str:
        return self.meta.get("name") or self.root.name

    @property
    def home(self) -> Path:
        return self.root / ".sentinel"

    @property
    def ledger(self) -> Ledger:
        return Ledger(self.home / "ledger.jsonl")

    @property
    def state(self) -> RunState:
        return RunState(self.home / "state.json", self.ledger)

    @property
    def runs_file(self) -> Path:
        return self.root / "config" / "runs.yaml"

    @property
    def rules_file(self) -> Path:
        return self.root / "config" / "rules.yaml"

    @property
    def accessions_dir(self) -> Path:
        return self.root / "data" / "accessions"

    @property
    def inventory_dir(self) -> Path:
        return self.home / "inventory"

    @property
    def metrics_dir(self) -> Path:
        return self.home / "metrics"

    def runs(self) -> dict[str, RunConfig]:
        """Validated runs from config/runs.yaml. Raises ConfigError if the file is wrong."""
        return load_runs(self.runs_file)

    def configured_runs(self) -> list[str]:
        return list(self.runs().keys())

    def rules(self) -> dict:
        if not self.rules_file.exists():
            return {}
        return yaml.safe_load(self.rules_file.read_text(encoding="utf-8")) or {}
