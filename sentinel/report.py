"""Stage 11: deterministic static HTML report per workspace (`sentinel report`).

Generated strictly from `.sentinel/ledger.jsonl`, `.sentinel/state.json`,
`.sentinel/metrics/*.json`, and `.qzv` files inside the workspace. Every number
cites its source file and sha256. Human entries (`actor == "human"`) are copied
literally. No AI-generated or interpretive text appears anywhere in the HTML.
"""

from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any

from .common import sha256_file, utc_iso, write_text_atomic
from .workspace import Workspace


def _esc(value: Any) -> str:
    return html.escape("" if value is None else str(value), quote=True)


def _code(value: Any) -> str:
    return f"<code>{_esc(value)}</code>"


def _svg_bar_chart(title: str, items: list[tuple[str, float, str]]) -> str:
    if not items:
        return ""
    max_val = max((v for _, v, _ in items), default=1.0) or 1.0
    bar_h = 22
    gap = 8
    label_w = 180
    bar_max_w = 340
    height = 28 + len(items) * (bar_h + gap)
    width = label_w + bar_max_w + 90
    rows: list[str] = [
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
            f'role="img" aria-label="{_esc(title)}">'
        ),
        f'<text x="0" y="16" font-size="13" font-weight="bold" fill="#222">{_esc(title)}</text>',
    ]
    for i, (label, val, unit) in enumerate(items):
        y = 26 + i * (bar_h + gap)
        w = max(2.0, (val / max_val) * bar_max_w) if val > 0 else 0.0
        rows.append(
            f'<text x="{label_w - 8}" y="{y + 15}" text-anchor="end" font-size="12" fill="#333">'
            f"{_esc(label)}</text>"
        )
        rows.append(
            f'<rect x="{label_w}" y="{y + 2}" width="{w:.1f}" height="{bar_h - 4}" '
            'fill="#2b6cb0" rx="2"/>'
        )
        val_txt = f"{val:g}{unit}" if unit else f"{val:g}"
        rows.append(
            f'<text x="{label_w + w + 6:.1f}" y="{y + 15}" font-size="12" fill="#222">'
            f"{_esc(val_txt)}</text>"
        )
    rows.append("</svg>")
    return "\n".join(rows)


def _collect_qzv(ws: Workspace) -> list[dict[str, Any]]:
    found: list[Path] = []
    for base in (ws.root / "runs", ws.home / "merge"):
        if base.exists():
            found.extend(base.rglob("*.qzv"))
    items: list[dict[str, Any]] = []
    for p in sorted(found, key=lambda x: x.as_posix()):
        if p.is_file():
            items.append(
                {
                    "rel": p.relative_to(ws.root).as_posix(),
                    "bytes": p.stat().st_size,
                    "sha256": sha256_file(p),
                }
            )
    return items


def render_report_html(ws: Workspace, *, generated_at: str | None = None) -> tuple[str, dict[str, Any]]:
    """Build deterministic static HTML from ledger, state, and metrics."""
    ts = generated_at or utc_iso()
    verify = ws.ledger.verify()
    entries = ws.ledger.entries()
    states = ws.state.all()
    state_sha = sha256_file(ws.state.path) if ws.state.path.exists() else None
    ledger_sha = sha256_file(ws.ledger.path) if ws.ledger.path.exists() else None

    counts: dict[str, int] = {}
    for st in states.values():
        counts[st] = counts.get(st, 0) + 1

    state_chart = _svg_bar_chart(
        "Runs by state",
        [(k, float(counts[k]), "") for k in sorted(counts)],
    )

    state_rows = "\n".join(
        f"<tr><td>{_code(r)}</td><td>{_code(states[r])}</td>"
        f"<td>{_code(ws.state.path.relative_to(ws.root).as_posix() if ws.state.path.exists() else '')} "
        f"(sha256 {_code((state_sha or '')[:16])})</td></tr>"
        for r in sorted(states)
    )

    metrics_sections: list[str] = []
    if ws.metrics_dir.exists():
        for mpath in sorted(ws.metrics_dir.glob("*.json"), key=lambda x: x.name):
            if mpath.name == "between.json":
                continue
            mdata = json.loads(mpath.read_text(encoding="utf-8"))
            run_name = mdata.get("run") or mpath.stem
            m_sha = sha256_file(mpath)
            vals = mdata.get("values") or {}
            pct_bars = [
                (k, float(v["value"]), "%")
                for k, v in sorted(vals.items())
                if isinstance(v, dict) and v.get("unit") == "%" and isinstance(v.get("value"), (int, float))
            ]
            trows: list[str] = []
            for k in sorted(vals):
                v = vals[k]
                src = v.get("source") or {}
                trows.append(
                    "<tr>"
                    f"<td>{_code(k)}</td>"
                    f"<td>{_esc(v.get('value'))} {_esc(v.get('unit') or '')}</td>"
                    f"<td>{_code(v.get('rule') or 'null')}</td>"
                    f"<td>{_code(v.get('result'))}</td>"
                    f"<td>{_code(src.get('path'))} (sha256 {_code((src.get('sha256') or '')[:16])})</td>"
                    "</tr>"
                )
            chart_html = _svg_bar_chart(f"{run_name} (%) metrics", pct_bars)
            metrics_sections.append(
                f"<h3>Run {_esc(run_name)}</h3>\n"
                f"<p>Source: {_code(mpath.relative_to(ws.root).as_posix())} "
                f"(sha256 {_code(m_sha)})</p>\n"
                f"{chart_html}\n"
                "<table><thead><tr><th>Metric</th><th>Value</th><th>Rule</th><th>Result</th><th>Source</th></tr></thead>"
                f"<tbody>{''.join(trows)}</tbody></table>"
            )

    between_html = "<p>No between-project metrics recorded.</p>"
    between_path = ws.metrics_dir / "between.json"
    if between_path.exists():
        bdata = json.loads(between_path.read_text(encoding="utf-8"))
        b_sha = sha256_file(between_path)
        b_rows: list[str] = []
        for label, st in sorted((bdata.get("results") or {}).items()):
            b_rows.append(
                "<tr>"
                f"<td>{_code(label)}</td>"
                f"<td>{_esc(st.get('R2'))}</td>"
                f"<td>{_esc(st.get('pseudo_F'))}</td>"
                f"<td>{_esc(st.get('p_value'))}</td>"
                f"<td>{_esc(st.get('permutations'))}</td>"
                f"<td>{_esc(bdata.get('seed'))}</td>"
                "</tr>"
            )
        between_html = (
            f"<p>Source: {_code(between_path.relative_to(ws.root).as_posix())} "
            f"(sha256 {_code(b_sha)})</p>\n"
            "<table><thead><tr><th>Factor</th><th>R2</th><th>pseudo-F</th><th>p</th><th>Permutations</th><th>Seed</th></tr></thead>"
            f"<tbody>{''.join(b_rows)}</tbody></table>"
        )

    human_entries = [e for e in entries if e.get("actor") == "human"]
    decision_rows: list[str] = []
    for e in human_entries:
        d = e.get("data") or {}
        literal_text = d.get("text") if e.get("event") == "decision" else d.get("justification")
        decision_rows.append(
            "<tr>"
            f"<td>{_esc(e.get('ts'))}</td>"
            f"<td>{_code(e.get('event'))}</td>"
            f"<td>{_code(d.get('run'))}</td>"
            f"<td>{_esc(e.get('user'))}</td>"
            f"<td><pre>{_esc(literal_text)}</pre></td>"
            f"<td>{_code((e.get('hash') or '')[:16])}</td>"
            "</tr>"
        )
    decisions_html = (
        "<p>No human decisions recorded.</p>"
        if not decision_rows
        else "<table><thead><tr><th>Timestamp</th><th>Event</th><th>Run</th><th>User</th><th>Literal text</th><th>Ledger hash</th></tr></thead>"
        f"<tbody>{''.join(decision_rows)}</tbody></table>"
    )

    qzv_items = _collect_qzv(ws)
    qzv_rows = "".join(
        f"<tr><td>{_code(q['rel'])}</td><td>{_esc(q['bytes'])}</td><td>{_code(q['sha256'])}</td></tr>"
        for q in qzv_items
    )
    qzv_html = (
        "<p>No .qzv artifacts found in workspace.</p>"
        if not qzv_rows
        else '<p>Open local files in <a href="https://view.qiime2.org">QIIME 2 View (view.qiime2.org)</a>.</p>'
        "<table><thead><tr><th>Path</th><th>Bytes</th><th>sha256</th></tr></thead>"
        f"<tbody>{qzv_rows}</tbody></table>"
    )

    title = ws.meta.get("title") or ws.name
    doc = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="generated-at" content="{_esc(ts)}">
<title>Sentinel Report: {_esc(title)}</title>
<style>
body {{ font-family: system-ui, -apple-system, sans-serif; margin: 2rem auto; max-width: 1080px; color: #1a202c; line-height: 1.5; }}
h1, h2, h3 {{ color: #1a365d; }}
table {{ border-collapse: collapse; width: 100%; margin: 1rem 0 1.5rem; font-size: 0.92rem; }}
th, td {{ border: 1px solid #cbd5e0; padding: 0.45rem 0.6rem; text-align: left; vertical-align: top; }}
th {{ background: #edf2f7; }}
code, pre {{ font-family: ui-monospace, SFMono-Regular, Menlo, monospace; font-size: 0.86rem; background: #f7fafc; padding: 0.1rem 0.25rem; }}
pre {{ margin: 0; white-space: pre-wrap; }}
</style>
</head>
<body>
<h1>Sentinel Report: {_esc(title)}</h1>
<p>Workspace: {_code(ws.name)} | Generated at: {_code(ts)}</p>
<p>Ledger verify: {_code('OK' if verify.ok else 'BROKEN')} ({verify.entries} entries) |
Ledger source: {_code(ws.ledger.path.relative_to(ws.root).as_posix() if ws.ledger.path.exists() else '')}
(sha256 {_code(ledger_sha or 'missing')})</p>

<h2>1. Run states</h2>
{state_chart}
<table>
<thead><tr><th>Run</th><th>State</th><th>Source</th></tr></thead>
<tbody>{state_rows}</tbody>
</table>

<h2>2. Run metrics</h2>
{''.join(metrics_sections) if metrics_sections else '<p>No run metrics recorded.</p>'}

<h2>3. Between-project metrics (PERMANOVA)</h2>
{between_html}

<h2>4. Human decisions (actor: human, verbatim)</h2>
{decisions_html}

<h2>5. QIIME 2 visualizations (.qzv)</h2>
{qzv_html}
</body>
</html>
"""
    return doc, {
        "workspace": ws.name,
        "generated_at": ts,
        "runs": len(states),
        "decisions": len(human_entries),
        "qzv_count": len(qzv_items),
        "ledger_entries": verify.entries,
        "ledger_ok": verify.ok,
    }


def generate_report(
    ws: Workspace,
    *,
    out_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """Generate and atomically write `.sentinel/report.html` (or `out_path`)."""
    target = out_path or (ws.home / "report.html")
    html_text, meta = render_report_html(ws, generated_at=generated_at)
    digest = write_text_atomic(target, html_text)
    return {
        **meta,
        "output": {"path": str(target), "sha256": digest},
    }
