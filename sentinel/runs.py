"""Validated configuration of sequencing runs (config/runs.yaml).

Each run may have the keys listed in RUN_KEYS. A missing key and a null value
mean the same thing: not filled in yet. `region` and `primers` come from each
study's methods, and the researcher fills them. Sentinel never fills in a
scientific value. Unknown keys are refused, and the error lists the accepted keys.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

import yaml

TOP_KEYS = ("runs",)
RUN_KEYS = ("accessions", "region", "primers", "drive_path", "metadata", "negative_controls")
PRIMER_KEYS = ("forward", "reverse")


class ConfigError(Exception):
    """runs.yaml does not match the schema. The message names the problem."""


@dataclass(frozen=True)
class Primers:
    forward: str | None
    reverse: str | None


@dataclass(frozen=True)
class RunConfig:
    name: str
    accessions: str | None
    region: str | None
    primers: Primers
    drive_path: str | None = None
    metadata: str | None = None
    negative_controls: tuple[str, ...] = ()


def _text_or_null(value: object, where: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{where}: esperado texto ou null")
    return value


def _str_list_or_null(value: object, where: str) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(x, str) or not x.strip() for x in value):
        raise ConfigError(f"{where}: esperado lista de textos ou null")
    return tuple(x.strip() for x in value)


def _unknown(keys: Iterable[object], accepted: tuple[str, ...]) -> list[str]:
    return sorted(str(k) for k in set(keys) if k not in accepted)


def parse_runs(data: object) -> dict[str, RunConfig]:
    """Check the parsed YAML and return {run name: RunConfig}."""
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ConfigError("runs.yaml: a raiz precisa ser um mapa com a chave 'runs'")
    unknown_top = _unknown(data, TOP_KEYS)
    if unknown_top:
        raise ConfigError(
            f"runs.yaml: chave desconhecida {unknown_top}; aceitas: {', '.join(TOP_KEYS)}"
        )
    runs_raw = data.get("runs") or {}
    if not isinstance(runs_raw, dict):
        raise ConfigError("runs.yaml: 'runs' precisa ser um mapa de corridas")

    out: dict[str, RunConfig] = {}
    for name, body in runs_raw.items():
        where = f"corrida {name}"
        if not isinstance(name, str) or not name.strip():
            raise ConfigError(f"runs.yaml: nome de corrida precisa ser texto: {name!r}")
        if not isinstance(body, dict):
            raise ConfigError(f"{where}: esperado um mapa com as chaves {', '.join(RUN_KEYS)}")
        unknown = _unknown(body, RUN_KEYS)
        if unknown:
            raise ConfigError(f"{where}: chave desconhecida {unknown}; aceitas: {', '.join(RUN_KEYS)}")
        primers_raw = body.get("primers") or {}
        if not isinstance(primers_raw, dict):
            raise ConfigError(f"{where}.primers: esperado um mapa com {', '.join(PRIMER_KEYS)}")
        bad_primer = _unknown(primers_raw, PRIMER_KEYS)
        if bad_primer:
            raise ConfigError(
                f"{where}.primers: chave desconhecida {bad_primer}; aceitas: {', '.join(PRIMER_KEYS)}"
            )

        out[name] = RunConfig(
            name=name,
            accessions=_text_or_null(body.get("accessions"), f"{where}.accessions"),
            region=_text_or_null(body.get("region"), f"{where}.region"),
            primers=Primers(
                forward=_text_or_null(primers_raw.get("forward"), f"{where}.primers.forward"),
                reverse=_text_or_null(primers_raw.get("reverse"), f"{where}.primers.reverse"),
            ),
            drive_path=_text_or_null(body.get("drive_path"), f"{where}.drive_path"),
            metadata=_text_or_null(body.get("metadata"), f"{where}.metadata"),
            negative_controls=_str_list_or_null(body.get("negative_controls"), f"{where}.negative_controls"),
        )
    return out


def load_runs(path: Path) -> dict[str, RunConfig]:
    """Read and validate config/runs.yaml. A missing file means no runs."""
    if not path.exists():
        return {}
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise ConfigError(f"runs.yaml não é YAML válido: {exc}") from None
    return parse_runs(data)
