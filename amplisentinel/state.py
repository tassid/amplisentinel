"""Per-run state machine.

A run (one sequencing run, for example P1 or P10a) moves through fixed states.
Only the transitions listed in TRANSITIONS are allowed. Every transition is
written to the ledger. BLOCKED can only be left by a human decision with a
written justification.
"""

from __future__ import annotations

import json
from pathlib import Path

from .ledger import Ledger

STATES = [
    "PENDING",
    "FETCHING",
    "MD5_OK",
    "PROCESSING",
    "MEASURED",
    "PUBLISHED",
    "CLEANED",
    "BLOCKED",
]

TRANSITIONS: dict[str, set[str]] = {
    "PENDING": {"FETCHING"},
    "FETCHING": {"MD5_OK", "BLOCKED"},
    "MD5_OK": {"PROCESSING"},
    "PROCESSING": {"MEASURED", "BLOCKED"},
    "MEASURED": {"PUBLISHED", "BLOCKED"},
    "PUBLISHED": {"CLEANED"},
    "CLEANED": set(),
    "BLOCKED": {"PENDING", "FETCHING", "PROCESSING", "MEASURED"},
}


class TransitionError(Exception):
    pass


class RunState:
    def __init__(self, path: Path, ledger: Ledger):
        self.path = Path(path)
        self.ledger = ledger
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _load(self) -> dict[str, str]:
        if not self.path.exists():
            return {}
        return json.loads(self.path.read_text(encoding="utf-8"))

    def _save(self, states: dict[str, str]) -> None:
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(states, indent=2, sort_keys=True), encoding="utf-8")
        tmp.replace(self.path)

    def all(self) -> dict[str, str]:
        return self._load()

    def get(self, run: str) -> str:
        return self._load().get(run, "PENDING")

    def register(self, runs: list[str]) -> list[str]:
        states = self._load()
        new = [r for r in runs if r not in states]
        for r in new:
            states[r] = "PENDING"
        if new:
            self._save(states)
            self.ledger.append("runs_registered", {"runs": new})
        return new

    def move(self, run: str, to: str, justification: str | None = None) -> None:
        if to not in STATES:
            raise TransitionError(f"estado desconhecido: {to}")
        current = self.get(run)
        if to not in TRANSITIONS[current]:
            raise TransitionError(f"{run}: transição {current} -> {to} não permitida")
        actor = "amplisentinel"
        if current == "BLOCKED":
            if not justification or not justification.strip():
                raise TransitionError(
                    f"{run}: sair de BLOCKED exige justificativa escrita pela pesquisadora"
                )
            actor = "human"
        states = self._load()
        states[run] = to
        self._save(states)
        self.ledger.append(
            "state_change",
            {"run": run, "from": current, "to": to, "justification": justification},
            actor=actor,
        )
