"""Render an audit bundle to a .pdf report (reportlab platypus)."""
from __future__ import annotations


def render_pdf(data: dict, path: str) -> str:
    try:
        from reportlab.lib import colors  # type: ignore
        from reportlab.lib.pagesizes import A4  # type: ignore
        from reportlab.lib.styles import getSampleStyleSheet  # type: ignore
        from reportlab.lib.units import cm  # type: ignore
        from reportlab.platypus import (  # type: ignore
            Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle,
        )
    except ImportError as e:
        raise RuntimeError(
            "reportlab is required for PDF reports — "
            "pip install reportlab (or datavigil[reports])") from e

    styles = getSampleStyleSheet()
    h1 = styles["Heading1"]
    h2 = styles["Heading2"]
    body = styles["BodyText"]
    italic = styles["Italic"]

    prof = data.get("profile") or {}
    run = data.get("run") or {}
    anom = data.get("anomalies") or {}
    analysis = data.get("analysis") or {}

    def esc(v) -> str:
        return str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def tbl(rows, widths=None):
        t = Table(rows, colWidths=widths, repeatRows=1)
        t.setStyle(TableStyle([
            ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2c3e50")),
            ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
            ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
            ("FONTSIZE", (0, 0), (-1, -1), 8),
            ("GRID", (0, 0), (-1, -1), 0.25, colors.HexColor("#b0b8c0")),
            ("ROWBACKGROUNDS", (0, 1), (-1, -1),
             [colors.white, colors.HexColor("#f2f5f8")]),
            ("VALIGN", (0, 0), (-1, -1), "TOP"),
            ("LEFTPADDING", (0, 0), (-1, -1), 4),
            ("RIGHTPADDING", (0, 0), (-1, -1), 4),
            ("TOPPADDING", (0, 0), (-1, -1), 3),
            ("BOTTOMPADDING", (0, 0), (-1, -1), 3),
        ]))
        return t

    story = []
    story.append(Paragraph("DataVigil — Data Quality Report", h1))
    story.append(Paragraph(
        f"Dataset: <b>{esc(data['dataset'])}</b> &nbsp;·&nbsp; "
        f"Generated: {esc(str(data['generated_at'])[:19].replace('T', ' '))} UTC",
        body))
    story.append(Spacer(1, 0.3 * cm))

    story.append(Paragraph("Executive summary", h2))
    rc = prof.get("row_count", run.get("row_count"))
    summary = [
        ["Metric", "Value"],
        ["Rows", f"{rc:,}" if isinstance(rc, int) else "—"],
        ["Columns", str(prof.get("column_count", "—"))],
        ["Duplicate rows", f"{prof.get('duplicate_row_count', 0):,}"],
        ["DQ score", f"{run.get('score', '—')} / 100"],
        ["Rules passed", f"{run.get('passed_rules', '—')} / {run.get('total_rules', '—')}"],
        ["Anomalies found", str(anom.get("count", "—"))],
        ["Overall severity", str(analysis.get("overall_severity", "—"))],
    ]
    story.append(tbl(summary, widths=[6 * cm, 9 * cm]))

    sampled = prof.get("profiled_rows")
    if sampled and prof.get("row_count") and sampled < prof["row_count"]:
        story.append(Paragraph(
            f"<i>Metrics computed on a sample of the first {sampled:,} of "
            f"{prof['row_count']:,} rows.</i>", italic))
    story.append(Spacer(1, 0.3 * cm))

    if analysis.get("summary"):
        story.append(Paragraph("Assessment", h2))
        story.append(Paragraph(esc(analysis["summary"]), body))
        issues = analysis.get("issues") or []
        if issues:
            rows = [["Issue", "Likely root cause", "Recommended fix", "Priority"]]
            for it in issues[:20]:
                rows.append([
                    Paragraph(esc(f"{it.get('rule','')} ({it.get('column') or 'table'})"), body),
                    Paragraph(esc(it.get("root_cause", "")), body),
                    Paragraph(esc(it.get("recommended_fix", "")), body),
                    Paragraph(esc(it.get("priority", "")), body),
                ])
            story.append(Spacer(1, 0.15 * cm))
            story.append(tbl(rows, widths=[4 * cm, 5.5 * cm, 5.5 * cm, 1.8 * cm]))
        story.append(Spacer(1, 0.3 * cm))

    if prof.get("llm_summary"):
        story.append(Paragraph("AI profile insights", h2))
        story.append(Paragraph(esc(prof["llm_summary"]), body))
        story.append(Spacer(1, 0.3 * cm))

    dims = run.get("dimension_scores") or {}
    if dims:
        story.append(Paragraph("Score by quality dimension", h2))
        rows = [["Dimension", "Score (0–100)"]]
        rows += [[k.capitalize(), str(v)] for k, v in dims.items()]
        story.append(tbl(rows, widths=[6 * cm, 9 * cm]))
        story.append(Spacer(1, 0.3 * cm))

    results = run.get("results") or []
    if results:
        story.append(Paragraph("Rule results", h2))
        rows = [["Status", "Rule", "Column", "Sev.", "Pass rate", "Failed values"]]
        for r in results:
            status = "PASS" if r["passed"] else "FAIL"
            rows.append([
                Paragraph(f'<font color="{"#1a7f37" if r["passed"] else "#cf222e"}">'
                          f"{status}</font>", body),
                Paragraph(esc(r["rule"]), body),
                Paragraph(esc(r["column"]), body),
                Paragraph(esc(r["severity"]), body),
                Paragraph(f"{r['pass_rate']}% ({r['rows_failed']})", body),
                Paragraph(esc(", ".join(r["examples"][:3])[:120]), body),
            ])
        story.append(tbl(rows, widths=[1.6 * cm, 4 * cm, 2.6 * cm, 1.8 * cm,
                                       2.6 * cm, 4.2 * cm]))
        story.append(Spacer(1, 0.3 * cm))

    cols = prof.get("columns") or []
    if cols:
        story.append(Paragraph("Column profile", h2))
        rows = [["Column", "Type", "Null %", "Distinct", "Range / Pattern", "Flags"]]
        for c in cols:
            rng = ""
            if c.get("min") is not None or c.get("max") is not None:
                rng = f"{c.get('min')} – {c.get('max')}"
            elif c.get("pattern"):
                rng = str(c["pattern"])[:50]
            rows.append([Paragraph(esc(c["name"]), body),
                         Paragraph(esc(c["type"]), body),
                         str(c["null_pct"]), f"{c['distinct']:,}",
                         Paragraph(esc(rng), body),
                         "unique" if c.get("unique") else ""])
        story.append(tbl(rows, widths=[3.5 * cm, 2.2 * cm, 1.6 * cm, 2 * cm,
                                       6 * cm, 1.7 * cm]))
        story.append(Spacer(1, 0.3 * cm))

    top = anom.get("top") or []
    if top:
        story.append(Paragraph("Top anomalies", h2))
        story.append(Paragraph(
            f"{anom.get('count', 0)} anomalies across {anom.get('total_rows', 0):,} rows "
            f"(methods: {esc(', '.join(anom.get('methods', [])))}). Top 25 shown:", body))
        rows = [["Method", "Column", "Value", "Why flagged"]]
        for a in top[:25]:
            rows.append([Paragraph(esc(a["method"]), body),
                         Paragraph(esc(a["column"]), body),
                         Paragraph(esc(a["value"]), body),
                         Paragraph(esc(a["reason"]), body)])
        story.append(tbl(rows, widths=[2.5 * cm, 3 * cm, 4.5 * cm, 7 * cm]))
        story.append(Spacer(1, 0.3 * cm))

    hist = data.get("history") or []
    if hist:
        story.append(Paragraph("Recent run history", h2))
        rows = [["When (UTC)", "DQ score", "Pass / Fail"]]
        for h_ in hist[:15]:
            rows.append([str(h_.get("started_at", ""))[:19].replace("T", " "),
                         str(h_.get("score", "—")),
                         f"{h_.get('passed_rules', '—')} / {h_.get('failed_rules', '—')}"])
        story.append(tbl(rows, widths=[6 * cm, 4 * cm, 5 * cm]))

    doc = SimpleDocTemplate(
        path, pagesize=A4, title="DataVigil Data Quality Report",
        leftMargin=1.6 * cm, rightMargin=1.6 * cm,
        topMargin=1.6 * cm, bottomMargin=1.6 * cm)
    doc.build(story)
    return path
