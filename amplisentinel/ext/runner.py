"""Thin layer over external tools (rclone now; Nextflow, Docker and others later).

Every external call goes through `call()`. It runs the command through a
Runner, saves stdout and stderr byte for byte under .amplisentinel/logs/, and writes
one `tool_call` event to the ledger with the exact command, the tool version,
the exit code and the sha256 of both outputs. A non-zero exit also writes an
`error` event that points to the saved stderr, and raises ToolError with the
path of that file. Tests replace the Runner with a fake, so no tool is needed.
"""

from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..workspace import Workspace


@dataclass(frozen=True)
class CallResult:
    returncode: int
    stdout: bytes
    stderr: bytes


@dataclass(frozen=True)
class CallRecord:
    """A finished call, with the saved log files and their hashes."""

    returncode: int
    stdout: bytes
    stderr: bytes
    stdout_path: str
    stderr_path: str
    stdout_sha256: str
    stderr_sha256: str
    version: str | None


Runner = Callable[[list[str]], CallResult]


class ToolError(Exception):
    """An external tool failed. Its literal stderr is in the saved log file."""

    def __init__(self, message: str, log: dict | None = None):
        super().__init__(message)
        self.log = log


def _unique_file(log_dir: Path, prefix: str, suffix: str = "") -> Path:
    candidate = log_dir / f"{prefix}{suffix}"
    n = 2
    while candidate.exists():
        candidate = log_dir / f"{prefix}-{n}{suffix}"
        n += 1
    return candidate


def _unique_log_pair(log_dir: Path, stamp: str, tool_name: str) -> tuple[Path, Path]:
    out_path = log_dir / f"{stamp}-{tool_name}.stdout"
    err_path = log_dir / f"{stamp}-{tool_name}.stderr"
    n = 2
    while out_path.exists() or err_path.exists():
        out_path = log_dir / f"{stamp}-{tool_name}-{n}.stdout"
        err_path = log_dir / f"{stamp}-{tool_name}-{n}.stderr"
        n += 1
    return out_path, err_path


def save_log(ws: Workspace, label: str, data: bytes) -> dict:
    """Save bytes under .amplisentinel/logs/ and return {"path", "sha256"}."""
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_dir = ws.home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    path = _unique_file(log_dir, f"{stamp}-{label}")
    path.write_bytes(data)
    return {"path": str(path), "sha256": hashlib.sha256(data).hexdigest()}


def subprocess_runner(argv: list[str]) -> CallResult:
    """Run a real command. A missing executable is reported as exit code 127."""
    try:
        proc = subprocess.run(argv, capture_output=True, check=False)
    except OSError as exc:
        return CallResult(returncode=127, stdout=b"", stderr=str(exc).encode("utf-8"))
    return CallResult(returncode=proc.returncode, stdout=proc.stdout, stderr=proc.stderr)


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def tool_version(version_argv: list[str], runner: Runner) -> str | None:
    """First line of the tool's version output, or None if it does not answer."""
    res = runner(version_argv)
    if res.returncode != 0:
        return None
    lines = res.stdout.decode("utf-8", errors="replace").splitlines()
    return lines[0].strip() if lines else None


def call(
    ws: Workspace,
    argv: list[str],
    *,
    version_argv: list[str],
    runner: Runner = subprocess_runner,
    context: dict | None = None,
    check: bool = True,
) -> CallRecord:
    """Run one external command, log it, and return its record.

    When check is True (default), raises ToolError on a non-zero exit code and
    writes both `tool_call` and `error` to the ledger. When check is False,
    writes `tool_call` and returns the CallRecord even on non-zero exit.
    """
    ctx = dict(context or {})
    tool = argv[0]
    tool_name = Path(tool).name or "tool"
    version = tool_version(version_argv, runner)
    res = runner(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_dir = ws.home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path, err_path = _unique_log_pair(log_dir, stamp, tool_name)
    out_path.write_bytes(res.stdout)
    err_path.write_bytes(res.stderr)

    record = CallRecord(
        returncode=res.returncode,
        stdout=res.stdout,
        stderr=res.stderr,
        stdout_path=str(out_path),
        stderr_path=str(err_path),
        stdout_sha256=_sha256(res.stdout),
        stderr_sha256=_sha256(res.stderr),
        version=version,
    )
    ws.ledger.append(
        "tool_call",
        {
            **ctx,
            "command": argv,
            "tool": tool,
            "version": version,
            "exit_code": res.returncode,
            "stdout": {"path": record.stdout_path, "sha256": record.stdout_sha256},
            "stderr": {"path": record.stderr_path, "sha256": record.stderr_sha256},
        },
    )
    if check and res.returncode != 0:
        ws.ledger.append(
            "error",
            {
                **ctx,
                "tool": tool,
                "command": argv,
                "exit_code": res.returncode,
                "stderr": {"path": record.stderr_path, "sha256": record.stderr_sha256},
            },
        )
        raise ToolError(
            f"{tool} saiu com código {res.returncode}; "
            f"stderr literal em {record.stderr_path} (sha256 {record.stderr_sha256})",
            log={"path": record.stderr_path, "sha256": record.stderr_sha256},
        )
    return record
