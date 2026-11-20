"""MCP server bound to one workspace (one research project).

Start one server per project, for example from OpenBot:

    amplisentinel --workspace ~/pesquisa/cervical-cancer mcp            # stdio
    amplisentinel --workspace ~/pesquisa/cervical-cancer mcp --http --port 8701

The workspace is fixed when the server starts. No tool takes a path, so a Bot
attached to this server cannot read any other project. Every tool is read only
and returns data with its source file and sha256. State changes and decisions
stay in the CLI, typed by the researcher.
"""

from __future__ import annotations

from typing import Any

from . import queries as q
from .workspace import Workspace

INSTRUCTIONS = """You are the AmpliSentinel interface for ONE research project: {name}.
Report only data returned by these tools: values, states, error messages, file
paths and hashes. Cite the source path for every number. Repeat error messages
literally. Compare values only against the rules returned in `rules_in_use`; if
there is no rule for a value, report the value alone. Never suggest parameters,
never say a result is good, bad, expected or suspicious, never recommend a next
step, and never write a decision or justification. This server only knows the
project {name}; for any other project, say it is outside this workspace."""


def build_server(ws: Workspace):
    from mcp.server.mcpserver import MCPServer
    from mcp.server.mcpserver.exceptions import ToolError
    from mcp.types import ToolAnnotations

    ro = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True, open_world_hint=False)

    def guarded(fn, *args):
        try:
            return fn(ws, *args)
        except q.QueryError as exc:
            raise ToolError(str(exc)) from None

    server = MCPServer(
        name=f"amplisentinel-{ws.name}",
        title=f"AmpliSentinel: {ws.meta.get('title') or ws.name}",
        instructions=INSTRUCTIONS.format(name=ws.name),
    )

    @server.tool(annotations=ro, description="Workspace name, title and configured sequencing runs.")
    def workspace() -> dict[str, Any]:
        return q.workspace_info(ws)

    @server.tool(annotations=ro, description="State of every sequencing run in this project, and counts by state.")
    def status() -> dict[str, Any]:
        return q.status(ws)

    @server.tool(annotations=ro, description="State and full ledger history of one sequencing run (for example P14).")
    def run_detail(run: str) -> dict[str, Any]:
        return guarded(q.run_detail, run)

    @server.tool(annotations=ro, description="Recorded metrics of one sequencing run, with the researcher's rules file.")
    def run_metrics(run: str) -> dict[str, Any]:
        return guarded(q.run_metrics, run)

    @server.tool(annotations=ro, description="Inventory per study (accessions, ENA layout, instruments, reads, GB). Optional study filter.")
    def inventory(study: str | None = None) -> dict[str, Any]:
        return q.inventory(ws, study)

    @server.tool(annotations=ro, description="Last n ledger entries (1 to 500).")
    def ledger_tail(n: int = 20) -> dict[str, Any]:
        return q.ledger_tail(ws, n)

    @server.tool(annotations=ro, description="Check the ledger hash chain.")
    def ledger_verify() -> dict[str, Any]:
        return q.ledger_verify(ws)

    @server.tool(annotations=ro, description="Error events from the ledger, optionally for one run, repeated literally.")
    def errors(run: str | None = None) -> dict[str, Any]:
        return guarded(q.errors, run)

    @server.tool(annotations=ro, description="ENA x Google Drive reconciliation rows, optionally filtered by run.")
    def reconciliation(run: str | None = None) -> dict[str, Any]:
        return guarded(q.reconciliation, run)

    @server.tool(annotations=ro, description="Verified FASTQ fetch manifest of one sequencing run.")
    def fetch_manifest(run: str) -> dict[str, Any]:
        return guarded(q.fetch_manifest, run)

    @server.tool(annotations=ro, description="Nextflow weblog process events (started, completed, failed) of one run.")
    def process_events(run: str) -> dict[str, Any]:
        return guarded(q.process_events, run)

    @server.tool(annotations=ro, description="Between-project PERMANOVA metrics (R2, pseudo-F, p-value, seed).")
    def between_metrics() -> dict[str, Any]:
        return q.between_metrics(ws)

    @server.tool(annotations=ro, description="Deterministic leave-one-project-out ML cross-validation folds.")
    def folds() -> dict[str, Any]:
        return q.folds(ws)

    return server


def serve(ws: Workspace, http: bool = False, host: str = "127.0.0.1", port: int = 8701) -> None:
    """stdio by default; with http=True, Streamable HTTP at http://host:port/mcp.
    Binds to 127.0.0.1 unless told otherwise, so only this computer can reach it."""
    server = build_server(ws)
    if http:
        server.run("streamable-http", host=host, port=port)
    else:
        server.run("stdio")
