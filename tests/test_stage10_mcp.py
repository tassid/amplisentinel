"""Stage 10: extended read-only MCP tools, workspace isolation, serve-all, and launchd."""

import asyncio
import hashlib
import json

import pytest

from sentinel import serve_all
from sentinel.cli import main
from sentinel.workspace import Workspace


def _populated_ws(tmp_path, name: str, run: str) -> Workspace:
    ws = Workspace.create(tmp_path / name, name=name)
    ws.runs_file.write_text(f"runs:\n  {run}: {{}}\n", encoding="utf-8")
    ws.state.register([run])
    ws.inventory_dir.mkdir(parents=True, exist_ok=True)
    (ws.inventory_dir / "reconciliation.tsv").write_text(
        f"run\taccession\tena_found\texpected_file\tena_md5\tena_bytes\tpresent_in_drive\tdrive_md5\tequal\n"
        f"{run}\tSRR1\tyes\tSRR1_1.fastq.gz\tabcd\t10\tyes\tabcd\tyes\n",
        encoding="utf-8",
    )
    run_dir = ws.root / "runs" / run
    run_dir.mkdir(parents=True, exist_ok=True)
    (run_dir / "fetch_manifest.json").write_text(
        json.dumps({"run": run, "files": [{"file": "SRR1_1.fastq.gz", "equal": True}]}),
        encoding="utf-8",
    )
    ws.ledger.append("process_started", {"run": run, "process": "DADA2_DENOISING"})
    ws.metrics_dir.mkdir(parents=True, exist_ok=True)
    (ws.metrics_dir / "between.json").write_text(json.dumps({"seed": 42, "workspace": name}), encoding="utf-8")
    (ws.home / "ml").mkdir(parents=True, exist_ok=True)
    (ws.home / "ml" / "folds.json").write_text(json.dumps({"folds": [{"fold": 0, "test_project": run}]}), encoding="utf-8")
    return ws


def test_mcp_all_13_tools_read_only_and_isolated_between_two_workspaces(tmp_path):
    pytest.importorskip("mcp")
    from mcp.server.mcpserver.exceptions import ToolError as McpToolError

    from sentinel.mcp_server import build_server

    ws_a = _populated_ws(tmp_path, "cervical", "P1")
    ws_b = _populated_ws(tmp_path, "rice", "R1")

    state_before = hashlib.sha256(ws_a.state.path.read_bytes()).hexdigest()
    ledger_before = hashlib.sha256(ws_a.ledger.path.read_bytes()).hexdigest()

    srv_a = build_server(ws_a)
    srv_b = build_server(ws_b)

    async def go():
        tools = await srv_a.list_tools()
        assert len(tools) == 13
        for t in tools:
            assert t.annotations is not None and t.annotations.read_only_hint is True
            assert t.annotations.destructive_hint is False
            for pname in (t.input_schema or {}).get("properties") or {}:
                assert not any(bad in pname.lower() for bad in ("path", "file", "dir", "folder"))

        rec = json.loads((await srv_a.call_tool("reconciliation", {"run": "P1"})).content[0].text)
        fm = json.loads((await srv_a.call_tool("fetch_manifest", {"run": "P1"})).content[0].text)
        pe = json.loads((await srv_a.call_tool("process_events", {"run": "P1"})).content[0].text)
        bm = json.loads((await srv_a.call_tool("between_metrics", {})).content[0].text)
        fl = json.loads((await srv_a.call_tool("folds", {})).content[0].text)
        assert rec["rows"][0]["expected_file"] == "SRR1_1.fastq.gz"
        assert fm["manifest"]["files"][0]["file"] == "SRR1_1.fastq.gz"
        assert pe["events"][0]["data"]["process"] == "DADA2_DENOISING"
        assert bm["between"]["workspace"] == "cervical"
        assert fl["folds"]["folds"][0]["test_project"] == "P1"

        # Workspace B never returns Workspace A's data
        bm_b = json.loads((await srv_b.call_tool("between_metrics", {})).content[0].text)
        assert bm_b["workspace"] == "rice" and bm_b["between"]["workspace"] == "rice"
        with pytest.raises(McpToolError):
            await srv_b.call_tool("fetch_manifest", {"run": "P1"})
        with pytest.raises(McpToolError):
            await srv_b.call_tool("process_events", {"run": "P1"})

    asyncio.run(go())

    # Neither state nor ledger was mutated by any MCP tool
    assert hashlib.sha256(ws_a.state.path.read_bytes()).hexdigest() == state_before
    assert hashlib.sha256(ws_a.ledger.path.read_bytes()).hexdigest() == ledger_before


def test_serve_all_config_and_launchd_plist(tmp_path):
    ws_a = Workspace.create(tmp_path / "cervical", name="cervical")
    ws_b = Workspace.create(tmp_path / "rice", name="rice")
    cfg = tmp_path / "workspaces.yaml"
    cfg.write_text(
        f"workspaces:\n  - path: '{ws_a.root}'\n    port: 8701\n  - path: '{ws_b.root}'\n    port: 8702\n",
        encoding="utf-8",
    )
    launched = serve_all.serve_all(cfg, launcher=lambda cmd, e: {"cmd": cmd, "name": e.workspace.name, "port": e.port})
    assert [(x["name"], x["port"]) for x in launched] == [("cervical", 8701), ("rice", 8702)]
    assert all("127.0.0.1" in x["cmd"] for x in launched)

    plist_out = tmp_path / "org.amplisentinel.mcp.cervical.plist"
    assert main(["-w", str(ws_a.root), "launchd", "--port", "8701", "--out", str(plist_out)]) == 0
    text = plist_out.read_text(encoding="utf-8")
    assert "org.amplisentinel.mcp.cervical" in text
    assert "8701" in text and "127.0.0.1" in text
