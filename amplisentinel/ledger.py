"""Append-only, hash-chained ledger.

Every event is one JSON line. Each line carries the hash of the previous line,
so editing or deleting any past line breaks the chain and `verify()` reports
the first line where it breaks.
"""

from __future__ import annotations

import getpass
import hashlib
import json
import os
import platform
import subprocess
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _canonical(obj: dict) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def git_commit(cwd: Path | None = None) -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=cwd, capture_output=True, text=True, check=True
        )
        return out.stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        return None


_THREAD_LOCK = threading.Lock()


@contextmanager
def _exclusive(lock_path: Path):
    """Cross-process lock around read-last-hash + append.

    Several processes write the same ledger (for example `amplisentinel run` and
    `amplisentinel listen` or `amplisentinel ui`). Without this lock two events could
    point to the same previous hash and break the chain.
    """
    with _THREAD_LOCK:
        fh = open(lock_path, "a+b")  # noqa: SIM115 (released in finally below)
        try:
            if os.name == "nt":
                import msvcrt

                fh.seek(0)
                while True:
                    try:
                        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
                        break
                    except OSError:
                        time.sleep(0.01)
                try:
                    yield
                finally:
                    fh.seek(0)
                    msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(fh.fileno(), fcntl.LOCK_EX)
                try:
                    yield
                finally:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
        finally:
            fh.close()


@dataclass
class VerifyResult:
    ok: bool
    entries: int
    broken_at: int | None = None
    reason: str | None = None


class Ledger:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def _last_hash(self) -> str:
        if not self.path.exists() or self.path.stat().st_size == 0:
            return GENESIS
        last = None
        with self.path.open("r", encoding="utf-8") as fh:
            for line in fh:
                if line.strip():
                    last = line
        return json.loads(last)["hash"] if last else GENESIS

    def append(self, event: str, data: dict | None = None, actor: str = "amplisentinel") -> dict:
        """Append one event. `actor` is "amplisentinel" for automatic events and
        "human" for decisions typed by the researcher."""
        if actor not in {"amplisentinel", "human"}:
            raise ValueError("actor must be 'amplisentinel' or 'human'")
        commit = git_commit(self.path.parent)
        with _exclusive(self.path.with_name(self.path.name + ".lock")):
            body = {
                "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "event": event,
                "actor": actor,
                "user": getpass.getuser(),
                "host": platform.node(),
                "git_commit": commit,
                "data": data or {},
                "prev": self._last_hash(),
            }
            body["hash"] = _sha256(_canonical(body))
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(_canonical(body) + "\n")
        return body

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        with self.path.open("r", encoding="utf-8") as fh:
            return [json.loads(line) for line in fh if line.strip()]

    def verify(self) -> VerifyResult:
        prev = GENESIS
        n = 0
        if not self.path.exists():
            return VerifyResult(ok=True, entries=0)
        with self.path.open("r", encoding="utf-8") as fh:
            for i, line in enumerate(fh, start=1):
                if not line.strip():
                    continue
                n += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    return VerifyResult(False, n, i, "linha não é JSON válido")
                stored = entry.pop("hash", None)
                if entry.get("prev") != prev:
                    return VerifyResult(False, n, i, "prev não corresponde ao hash da linha anterior")
                if _sha256(_canonical(entry)) != stored:
                    return VerifyResult(False, n, i, "conteúdo da linha foi alterado")
                prev = stored
        return VerifyResult(ok=True, entries=n)
