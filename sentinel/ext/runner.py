"""Thin layer over external tools (rclone now; Nextflow, Docker and others later).

Every external call goes through `call()`. It runs the command through a
Runner, saves stdout and stderr byte for byte under .sentinel/logs/, and writes
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
) -> CallRecord:
    """Run one external command, log it, and return its record.

    Raises ToolError when the exit code is not zero. The ledger keeps both the
    `tool_call` and the `error` event in that case.
    """
    tool = argv[0]
    version = tool_version(version_argv, runner)
    res = runner(argv)

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    log_dir = ws.home / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    out_path = log_dir / f"{stamp}-{tool}.stdout"
    err_path = log_dir / f"{stamp}-{tool}.stderr"
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
            "command": argv,
            "tool": tool,
            "version": version,
            "exit_code": res.returncode,
            "stdout": {"path": record.stdout_path, "sha256": record.stdout_sha256},
            "stderr": {"path": record.stderr_path, "sha256": record.stderr_sha256},
        },
    )
    if res.returncode != 0:
        ws.ledger.append(
            "error",
            {
                "tool": tool,
                "command": argv,
                "exit_code": res.returncode,
                "stderr": {"path": record.stderr_path, "sha256": record.stderr_sha256},
            },
        )
        raise ToolError(
            f"{tool} saiu com código {res.returncode}; "
            f"stderr literal em {record.stderr_path} (sha256 {record.stderr_sha256})"
        )
    return record
