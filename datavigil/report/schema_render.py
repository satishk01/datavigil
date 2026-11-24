"""Render a schema-audit bundle (multiple tables) to DOCX / PDF."""
from __future__ import annotations


def _sev(data_audit: dict) -> str:
    return str((data_audit.get("analysis") or {}).get("overall_severity") or "—")


def render_schema_docx(data: dict, path: str) -> str:
    try:
        from docx import Document  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "python-docx is required for DOCX reports — "
            "pip install python-docx (or datavigil[reports])") from e

    doc = Document()
    doc.add_heading("DataVigil — Schema Audit Report", level=0)
    sub = doc.add_paragraph()
    sub.add_run(
        f"Source: {data['dataset']}\n"
        f"Generated: {str(data['generated_at'])[:19].replace('T', ' ')} UTC"
    ).italic = True

    s = data.get("summary") or {}
    doc.add_heading("Executive summary", level=1)
    tbl = doc.add_table(rows=0, cols=2)
    tbl.style = "Light Grid Accent 1"
    for k, v in [
        ("Tables found", s.get("tables_found", "—")),
        ("Tables audited", s.get("tables_audited", "—")),
        ("Tables failed to audit", s.get("tables_failed", "—")),
        ("Average DQ score", f"{s.get('avg_score', '—')} / 100"),
        ("Lowest table score", f"{s.get('min_score', '—')} / 100"),
        ("Rules run / failed",
         f"{s.get('total_rules', '—')} / {s.get('total_failed_rules', '—')}"),
        ("Anomalies found", s.get("total_anomalies", "—")),
    ]:
        cells = tbl.add_row().cells
        cells[0].text = str(k)
        cells[1].text = str(v)

    tables = data.get("tables") or []
    doc.add_heading("Score by table (worst first)", level=1)
    t = doc.add_table(rows=1, cols=7)
    t.style = "Light Grid Accent 1"
    for i, h in enumerate(("Table", "Score", "Rows", "Cols",
                           "Rules failed", "Anomalies", "Severity")):
        t.rows[0].cells[i].text = h
    for row in tables:
        cells = t.add_row().cells
        cells[0].text = str(row["dataset"])
        cells[1].text = (f"{row['score']} / 100" if row["score"] is not None
                         else "—")
        cells[2].text = (f"{row['rows']:,}" if isinstance(row["rows"], int)
                         else "—")
        cells[3].text = str(row["columns"] if row["columns"] is not None else "—")
        cells[4].text = f"{row['rules_failed']} / {row['rules_total']}"
        cells[5].text = str(row["anomalies"])
        cells[6].text = str(row["severity"] or "—")

    errors = data.get("errors") or []
    if errors:
        doc.add_heading("Tables that could not be audited", level=1)
        t = doc.add_table(rows=1, cols=2)
        t.style = "Light Grid Accent 1"
        t.rows[0].cells[0].text = "Table"
        t.rows[0].cells[1].text = "Error"
        for e in errors:
            cells = t.add_row().cells
            cells[0].text = str(e["dataset"])
            cells[1].text = str(e["error"])[:300]

    # condensed per-table detail — failures + AI issues only
    audits = data.get("audits") or {}
    doc.add_heading("Findings by table", level=1)
    for ds, a in audits.items():
        run = a.get("run") or {}
        doc.add_heading(f"{ds} — score {run.get('score', '—')} / 100", level=2)
        failed = [r for r in (run.get("results") or []) if not r.get("passed")]
        if not failed:
            doc.add_paragraph("All quality rules passed.")
        else:
            t = doc.add_table(rows=1, cols=5)
            t.style = "Light Grid Accent 1"
            for i, h in enumerate(("Rule", "Column", "Severity",
                                   "Pass rate", "Failed values")):
                t.rows[0].cells[i].text = h
            for r in failed:
                cells = t.add_row().cells
                cells[0].text = str(r["rule"])
                cells[1].text = str(r["column"])
                cells[2].text = str(r["severity"])
                cells[3].text = f"{r['pass_rate']}% ({r['rows_failed']} rows)"
                cells[4].text = ", ".join(r["examples"][:3])[:150]
        issues = (a.get("analysis") or {}).get("issues") or []
        for it in issues[:3]:
            p = doc.add_paragraph(style="List Bullet")
            p.add_run(f"[{it.get('priority', '')}] {it.get('rule', '')}: "
                      f"{it.get('recommended_fix', '')}")

    doc.save(path)
    return path


def render_schema_pdf(data: dict, path: str) -> str:
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
    h1, h2, h3 = styles["Heading1"], styles["Heading2"], styles["Heading3"]
    body = styles["BodyText"]

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

    s = data.get("summary") or {}
    story = [
        Paragraph("DataVigil — Schema Audit Report", h1),
        Paragraph(
            f"Source: <b>{esc(data['dataset'])}</b> &nbsp;·&nbsp; "
            f"Generated: {esc(str(data['generated_at'])[:19].replace('T', ' '))} UTC",
            body),
        Spacer(1, 0.3 * cm),
        Paragraph("Executive summary", h2),
        tbl([
            ["Metric", "Value"],
            ["Tables found", str(s.get("tables_found", "—"))],
            ["Tables audited", str(s.get("tables_audited", "—"))],
            ["Tables failed to audit", str(s.get("tables_failed", "—"))],
            ["Average DQ score", f"{s.get('avg_score', '—')} / 100"],
            ["Lowest table score", f"{s.get('min_score', '—')} / 100"],
            ["Rules run / failed",
             f"{s.get('total_rules', '—')} / {s.get('total_failed_rules', '—')}"],
            ["Anomalies found", str(s.get("total_anomalies", "—"))],
        ], widths=[6 * cm, 9 * cm]),
        Spacer(1, 0.3 * cm),
    ]

    tables = data.get("tables") or []
    story.append(Paragraph("Score by table (worst first)", h2))
    rows = [["Table", "Score", "Rows", "Cols", "Rules failed",
             "Anomalies", "Severity"]]
    for r in tables:
        rows.append([
            Paragraph(esc(r["dataset"]), body),
            f"{r['score']}" if r["score"] is not None else "—",
            f"{r['rows']:,}" if isinstance(r["rows"], int) else "—",
            str(r["columns"] if r["columns"] is not None else "—"),
            f"{r['rules_failed']} / {r['rules_total']}",
            str(r["anomalies"]),
            Paragraph(esc(r["severity"] or "—"), body),
        ])
    story.append(tbl(rows, widths=[4.5 * cm, 1.8 * cm, 2 * cm, 1.4 * cm,
                                   2.4 * cm, 2 * cm, 2.7 * cm]))
    story.append(Spacer(1, 0.3 * cm))

    errors = data.get("errors") or []
    if errors:
        story.append(Paragraph("Tables that could not be audited", h2))
        rows = [["Table", "Error"]]
        for e in errors:
            rows.append([Paragraph(esc(e["dataset"]), body),
                         Paragraph(esc(str(e["error"])[:300]), body)])
        story.append(tbl(rows, widths=[5 * cm, 11 * cm]))
        story.append(Spacer(1, 0.3 * cm))

    audits = data.get("audits") or {}
    story.append(Paragraph("Findings by table", h2))
    for ds, a in audits.items():
        run = a.get("run") or {}
        story.append(Spacer(1, 0.15 * cm))
        story.append(Paragraph(
            f"{esc(ds)} — score {run.get('score', '—')} / 100", h3))
        failed = [r for r in (run.get("results") or []) if not r.get("passed")]
        if not failed:
            story.append(Paragraph("All quality rules passed.", body))
        else:
            rows = [["Rule", "Column", "Sev.", "Pass rate", "Failed values"]]
            for r in failed:
                rows.append([
                    Paragraph(esc(r["rule"]), body),
                    Paragraph(esc(r["column"]), body),
                    Paragraph(esc(r["severity"]), body),
                    Paragraph(f"{r['pass_rate']}% ({r['rows_failed']})", body),
                    Paragraph(esc(", ".join(r["examples"][:3])[:120]), body),
                ])
            story.append(tbl(rows, widths=[4 * cm, 2.6 * cm, 1.6 * cm,
                                           2.6 * cm, 6 * cm]))
        issues = (a.get("analysis") or {}).get("issues") or []
        for it in issues[:3]:
            story.append(Paragraph(
                f"• [{esc(it.get('priority', ''))}] {esc(it.get('rule', ''))}: "
                f"{esc(it.get('recommended_fix', ''))}", body))

    doc = SimpleDocTemplate(
        path, pagesize=A4, title="DataVigil Schema Audit Report",
        leftMargin=1.6 * cm, rightMargin=1.6 * cm,
        topMargin=1.6 * cm, bottomMargin=1.6 * cm)
    doc.build(story)
    return path
