import asyncio
import json

import pytest

from amplisentinel import queries as q
from amplisentinel.cli import main
from amplisentinel.workspace import Workspace, WorkspaceError


def _make(tmp_path, name, runs):
    ws = Workspace.create(tmp_path / name, name)
    ws.runs_file.write_text("runs:\n" + "".join(f"  {r}: {{}}\n" for r in runs))
    ws.state.register(ws.configured_runs())
    return ws


def test_workspaces_are_separate(tmp_path):
    a = _make(tmp_path, "cervical", ["P1", "P14"])
    b = _make(tmp_path, "rice", ["R1"])
    a.state.move("P1", "FETCHING")
    assert q.status(a)["runs"] == {"P1": "FETCHING", "P14": "PENDING"}
    assert q.status(b)["runs"] == {"R1": "PENDING"}
    assert a.ledger.path != b.ledger.path
    with pytest.raises(q.QueryError):
        q.run_detail(b, "P1")


def test_open_requires_marker(tmp_path):
    with pytest.raises(WorkspaceError):
        Workspace.open(tmp_path)


def test_queries_cite_source(tmp_path):
    ws = _make(tmp_path, "cervical", ["P1"])
    out = q.run_metrics(ws, "P1")
    assert out["metrics"] is None
    assert out["source"]["exists"] is False
    (ws.metrics_dir).mkdir(parents=True)
    (ws.metrics_dir / "P1.json").write_text(json.dumps({"dada2_merged_pct": 81.2}))
    out = q.run_metrics(ws, "P1")
    assert out["metrics"]["dada2_merged_pct"] == 81.2
    assert len(out["source"]["sha256"]) == 64


def test_cli_with_workspace_flag(tmp_path, capsys):
    root = tmp_path / "coral"
    assert main(["new", str(root), "--name", "coral"]) == 0
    (root / "config" / "runs.yaml").write_text("runs:\n  C1: {}\n")
    assert main(["-w", str(root), "init"]) == 0
    assert main(["-w", str(root), "status"]) == 0
    assert "C1  PENDING" in capsys.readouterr().out
    assert main(["-w", str(root), "verify"]) == 0


def test_mcp_tools_are_read_only_and_scoped(tmp_path):
    pytest.importorskip("mcp")
    from amplisentinel.mcp_server import build_server

    ws = _make(tmp_path, "cervical", ["P1"])
    server = build_server(ws)

    async def go():
        tools = await server.list_tools()
        names = {t.name for t in tools}
        assert names == {
            "workspace", "status", "run_detail", "run_metrics",
            "inventory", "ledger_tail", "ledger_verify", "errors",
            "reconciliation", "fetch_manifest", "process_events",
            "between_metrics", "folds",
        }
        forbidden_param_names = ("path", "file", "dir", "folder")
        for t in tools:
            assert t.annotations is not None and t.annotations.read_only_hint is True
            props = (t.input_schema or {}).get("properties") or {}
            for pname in props:
                assert not any(bad in pname.lower() for bad in forbidden_param_names)
        return await server.call_tool("status", {})

    result = asyncio.run(go())
    assert "P1" in json.dumps(result, default=str)
