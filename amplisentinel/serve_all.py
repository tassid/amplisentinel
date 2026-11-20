"""Multi-workspace MCP server runner (`amplisentinel serve-all`) and macOS launchd templates."""

from __future__ import annotations

import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

import yaml

from .runs import ConfigError
from .workspace import Workspace

DEFAULT_WORKSPACES_CONFIG = Path.home() / ".amplisentinel" / "workspaces.yaml"


@dataclass(frozen=True)
class ServerEntry:
    workspace: Workspace
    port: int
    host: str = "127.0.0.1"


def load_workspaces_config(path: Path | None = None) -> list[ServerEntry]:
    """Read and validate `~/.amplisentinel/workspaces.yaml`."""
    cfg_path = Path(path or DEFAULT_WORKSPACES_CONFIG).expanduser()
    if not cfg_path.exists():
        raise ConfigError(f"arquivo de configuração não encontrado: {cfg_path}")
    try:
        raw = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{cfg_path} não é YAML válido: {exc}") from None

    items = raw.get("workspaces") if isinstance(raw, dict) else None
    if not isinstance(items, list) or not items:
        raise ConfigError(f"{cfg_path}: esperado mapa com lista não vazia em 'workspaces'")

    entries: list[ServerEntry] = []
    seen_ports: set[int] = set()
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            raise ConfigError(f"{cfg_path}: entrada #{i + 1} precisa ser um mapa {{path, port}}")
        raw_path = item.get("path")
        port = item.get("port")
        host = item.get("host", "127.0.0.1")
        if not isinstance(raw_path, str) or not raw_path.strip():
            raise ConfigError(f"{cfg_path}: entrada #{i + 1} sem 'path'")
        if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
            raise ConfigError(f"{cfg_path}: entrada #{i + 1} precisa de 'port' entre 1 e 65535")
        if host != "127.0.0.1":
            raise ConfigError(f"{cfg_path}: entrada #{i + 1} aceita apenas host 127.0.0.1")
        if port in seen_ports:
            raise ConfigError(f"{cfg_path}: porta duplicada {port}")
        seen_ports.add(port)
        ws = Workspace.open(raw_path)
        entries.append(ServerEntry(workspace=ws, port=port, host=host))
    return entries


def build_commands(entries: list[ServerEntry], *, python_exe: str | None = None) -> list[list[str]]:
    exe = python_exe or sys.executable
    return [
        [
            exe,
            "-m",
            "amplisentinel.cli",
            "-w",
            str(entry.workspace.root),
            "mcp",
            "--http",
            "--host",
            entry.host,
            "--port",
            str(entry.port),
        ]
        for entry in entries
    ]


def serve_all(config_path: Path | None = None, *, launcher: Any = None) -> list[dict[str, Any]]:
    """Launch one HTTP MCP server per configured workspace on 127.0.0.1."""
    entries = load_workspaces_config(config_path)
    cmds = build_commands(entries)
    if launcher is not None:
        return [launcher(cmd, entry) for cmd, entry in zip(cmds, entries)]

    procs = [subprocess.Popen(cmd) for cmd in cmds]
    for entry in entries:
        print(f"MCP {entry.workspace.name}: http://{entry.host}:{entry.port}/mcp", flush=True)
    try:
        while any(p.poll() is None for p in procs):
            time.sleep(0.5)
    except KeyboardInterrupt:
        pass
    finally:
        for p in procs:
            if p.poll() is None:
                p.terminate()
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    return [{"workspace": e.workspace.name, "port": e.port} for e in entries]


def render_launchd_plist(
    ws: Workspace,
    *,
    port: int,
    amplisentinel_bin: str = "/opt/homebrew/bin/amplisentinel",
    host: str = "127.0.0.1",
) -> str:
    """Render a macOS launchd user agent plist for one workspace MCP server."""
    if host != "127.0.0.1":
        raise ConfigError("launchd plist aceita apenas host 127.0.0.1")
    if isinstance(port, bool) or not isinstance(port, int) or not (1 <= port <= 65535):
        raise ConfigError("porta inválida para launchd plist")
    label = f"org.amplisentinel.mcp.{ws.name}"
    ws_root = escape(str(ws.root))
    log_out = escape(str(ws.home / "logs" / "mcp.stdout.log"))
    log_err = escape(str(ws.home / "logs" / "mcp.stderr.log"))
    bin_esc = escape(amplisentinel_bin)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" '
        '"http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
        '<plist version="1.0">\n'
        "<dict>\n"
        "  <key>Label</key>\n"
        f"  <string>{escape(label)}</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"    <string>{bin_esc}</string>\n"
        "    <string>-w</string>\n"
        f"    <string>{ws_root}</string>\n"
        "    <string>mcp</string>\n"
        "    <string>--http</string>\n"
        "    <string>--host</string>\n"
        f"    <string>{host}</string>\n"
        "    <string>--port</string>\n"
        f"    <string>{port}</string>\n"
        "  </array>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "  <key>KeepAlive</key>\n"
        "  <true/>\n"
        "  <key>StandardOutPath</key>\n"
        f"  <string>{log_out}</string>\n"
        "  <key>StandardErrorPath</key>\n"
        f"  <string>{log_err}</string>\n"
        "</dict>\n"
        "</plist>\n"
    )
