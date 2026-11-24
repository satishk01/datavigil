"""Render an audit bundle to a .docx report (python-docx)."""
from __future__ import annotations


def render_docx(data: dict, path: str) -> str:
    try:
        from docx import Document  # type: ignore
        from docx.enum.text import WD_ALIGN_PARAGRAPH  # type: ignore
        from docx.shared import Inches, Pt, RGBColor  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "python-docx is required for DOCX reports — "
            "pip install python-docx (or datavigil[reports])") from e

    doc = Document()

    title = doc.add_heading(f"DataVigil — Data Quality Report", level=0)
    sub = doc.add_paragraph()
    sub.add_run(f"Dataset: {data['dataset']}\n"
                f"Generated: {str(data['generated_at'])[:19].replace('T', ' ')} UTC") \
        .italic = True

    # ---- executive summary ----
    doc.add_heading("Executive summary", level=1)
    prof = data.get("profile") or {}
    run = data.get("run") or {}
    anom = data.get("anomalies") or {}
    analysis = data.get("analysis") or {}

    tbl = doc.add_table(rows=0, cols=2)
    tbl.style = "Light Grid Accent 1"
    rows = [
        ("Rows", f"{prof.get('row_count', run.get('row_count', '—')):,}"
                 if isinstance(prof.get('row_count', run.get('row_count')), int) else "—"),
        ("Columns", str(prof.get("column_count", "—"))),
        ("Duplicate rows", f"{prof.get('duplicate_row_count', 0):,}"),
        ("DQ score", f"{run.get('score', '—')} / 100"),
        ("Rules passed", f"{run.get('passed_rules', '—')} / {run.get('total_rules', '—')}"),
        ("Anomalies found", f"{anom.get('count', '—')}"),
        ("Overall severity", str(analysis.get("overall_severity", "—"))),
    ]
    for k, v in rows:
        cells = tbl.add_row().cells
        cells[0].text = k
        cells[1].text = v

    sampled = prof.get("profiled_rows")
    if sampled and prof.get("row_count") and sampled < prof["row_count"]:
        p = doc.add_paragraph()
        p.add_run(f"Note: metrics computed on a sample of the first "
                  f"{sampled:,} of {prof['row_count']:,} rows.").italic = True

    # ---- AI / heuristic assessment ----
    if analysis.get("summary"):
        doc.add_heading("Assessment", level=1)
        doc.add_paragraph(analysis["summary"])
        issues = analysis.get("issues") or []
        if issues:
            t = doc.add_table(rows=1, cols=4)
            t.style = "Light Grid Accent 1"
            for i, h in enumerate(("Issue", "Likely root cause",
                                   "Recommended fix", "Priority")):
                t.rows[0].cells[i].text = h
            for it in issues[:20]:
                cells = t.add_row().cells
                cells[0].text = f"{it.get('rule', '')} ({it.get('column') or 'table'})"
                cells[1].text = str(it.get("root_cause", ""))
                cells[2].text = str(it.get("recommended_fix", ""))
                cells[3].text = str(it.get("priority", ""))

    if prof.get("llm_summary"):
        doc.add_heading("AI profile insights", level=1)
        doc.add_paragraph(prof["llm_summary"])

    # ---- dimension scores ----
    dims = run.get("dimension_scores") or {}
    if dims:
        doc.add_heading("Score by quality dimension", level=1)
        t = doc.add_table(rows=1, cols=2)
        t.style = "Light Grid Accent 1"
        t.rows[0].cells[0].text = "Dimension"
        t.rows[0].cells[1].text = "Score (0–100)"
        for k, v in dims.items():
            cells = t.add_row().cells
            cells[0].text = k.capitalize()
            cells[1].text = str(v)

    # ---- rule results ----
    results = run.get("results") or []
    if results:
        doc.add_heading("Rule results", level=1)
        t = doc.add_table(rows=1, cols=6)
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(("Status", "Rule", "Column", "Severity",
                               "Pass rate", "Failed values")):
            t.rows[0].cells[i].text = h
        for r in results:
            cells = t.add_row().cells
            cells[0].text = "PASS" if r["passed"] else "FAIL"
            cells[1].text = str(r["rule"])
            cells[2].text = str(r["column"])
            cells[3].text = str(r["severity"])
            cells[4].text = f"{r['pass_rate']}% ({r['rows_failed']} rows)"
            cells[5].text = ", ".join(r["examples"][:3])[:150]

    # ---- column profile ----
    cols = prof.get("columns") or []
    if cols:
        doc.add_heading("Column profile", level=1)
        t = doc.add_table(rows=1, cols=6)
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(("Column", "Type", "Null %", "Distinct",
                               "Range / Pattern", "Flags")):
            t.rows[0].cells[i].text = h
        for c in cols:
            cells = t.add_row().cells
            cells[0].text = str(c["name"])
            cells[1].text = str(c["type"])
            cells[2].text = str(c["null_pct"])
            cells[3].text = f"{c['distinct']:,}"
            rng = ""
            if c.get("min") is not None or c.get("max") is not None:
                rng = f"{c.get('min')} – {c.get('max')}"
            elif c.get("pattern"):
                rng = str(c["pattern"])[:60]
            cells[4].text = rng
            cells[5].text = "unique" if c.get("unique") else ""

    # ---- correlations ----
    corr = prof.get("correlations") or []
    if corr:
        doc.add_heading("Correlated columns", level=1)
        t = doc.add_table(rows=1, cols=3)
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(("Column A", "Column B", "Pearson r")):
            t.rows[0].cells[i].text = h
        for c in corr[:20]:
            cells = t.add_row().cells
            cells[0].text = str(c["col_a"])
            cells[1].text = str(c["col_b"])
            cells[2].text = str(c["coefficient"])

    # ---- anomalies ----
    top = anom.get("top") or []
    if top:
        doc.add_heading("Top anomalies", level=1)
        doc.add_paragraph(
            f"{anom.get('count', 0)} anomalies detected across "
            f"{anom.get('total_rows', 0):,} rows "
            f"(methods: {', '.join(anom.get('methods', []))}). "
            "Top 25 shown:")
        t = doc.add_table(rows=1, cols=4)
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(("Method", "Column", "Value", "Why flagged")):
            t.rows[0].cells[i].text = h
        for a in top[:25]:
            cells = t.add_row().cells
            cells[0].text = str(a["method"])
            cells[1].text = str(a["column"])
            cells[2].text = str(a["value"])
            cells[3].text = str(a["reason"])

    # ---- history ----
    hist = data.get("history") or []
    if hist:
        doc.add_heading("Recent run history", level=1)
        t = doc.add_table(rows=1, cols=3)
        t.style = "Light Grid Accent 1"
        for i, h in enumerate(("When (UTC)", "DQ score", "Rules pass/fail")):
            t.rows[0].cells[i].text = h
        for h_ in hist[:15]:
            cells = t.add_row().cells
            cells[0].text = str(h_.get("started_at", ""))[:19].replace("T", " ")
            cells[1].text = str(h_.get("score", "—"))
            cells[2].text = f"{h_.get('passed_rules', '—')} / {h_.get('failed_rules', '—')}"

    doc.save(path)
    return path
