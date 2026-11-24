"""Assemble the plain-dict audit bundle that feeds the DOCX/PDF renderers."""
from __future__ import annotations

import datetime as _dt
from typing import Optional

from ..core.models import (
    AnomalyResult, RunResult, TableProfile, to_jsonable,
)


def build_report_data(
    dataset: str,
    profile: Optional[TableProfile] = None,
    run: Optional[RunResult] = None,
    anomalies: Optional[AnomalyResult] = None,
    analysis: Optional[dict] = None,
    history: Optional[list[dict]] = None,
) -> dict:
    """Normalise profile/run/anomaly/AI results into one report-ready dict."""
    data: dict = {
        "title": "DataVigil Data Quality Report",
        "dataset": dataset,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "history": history or [],
    }

    if profile:
        data["profile"] = {
            "row_count": profile.row_count,
            "profiled_rows": getattr(profile, "profiled_rows", None) or profile.row_count,
            "column_count": profile.column_count,
            "duplicate_row_count": profile.duplicate_row_count,
            "correlated_pairs": len(profile.correlations),
            "correlations": to_jsonable(profile.correlations),
            "llm_summary": profile.llm_summary,
            "columns": [
                {
                    "name": c.name,
                    "type": c.inferred_type,
                    "null_pct": c.null_pct,
                    "distinct": c.distinct_count,
                    "unique": c.is_unique,
                    "min": c.min if c.min is not None else c.min_date,
                    "max": c.max if c.max is not None else c.max_date,
                    "pattern": (c.patterns[0]["pattern"] if c.patterns else ""),
                }
                for c in profile.columns
            ],
        }

    if run:
        data["run"] = {
            "score": run.score,
            "dimension_scores": run.dimension_scores,
            "total_rules": run.total_rules,
            "passed_rules": run.passed_rules,
            "failed_rules": run.failed_rules,
            "row_count": run.row_count,
            "finished_at": run.finished_at,
            "results": [
                {
                    "passed": r.passed,
                    "rule": r.rule_name,
                    "column": r.column or "—",
                    "dimension": (r.dimension.value if hasattr(r.dimension, "value")
                                  else r.dimension),
                    "severity": (r.severity.value if hasattr(r.severity, "value")
                                 else r.severity),
                    "pass_rate": round(r.pass_rate * 100, 1),
                    "rows_failed": r.rows_failed,
                    "examples": [str(e) for e in r.failed_examples[:4]],
                    "error": r.error,
                }
                for r in run.results
            ],
        }

    if anomalies:
        data["anomalies"] = {
            "count": anomalies.anomaly_count,
            "total_rows": anomalies.total_rows,
            "methods": anomalies.methods_used,
            "top": [
                {
                    "method": a.method,
                    "column": a.column or "(row)",
                    "score": a.score,
                    "value": str(a.value)[:60],
                    "reason": a.reason,
                }
                for a in anomalies.anomalies[:50]
            ],
        }

    if analysis:
        data["analysis"] = analysis

    return data


def build_schema_report_data(
    source: str,
    audits: dict[str, dict],
    errors: Optional[dict[str, str]] = None,
    schema: Optional[str] = None,
) -> dict:
    """Aggregate per-dataset audit bundles into a schema-level bundle.

    `audits` maps dataset name -> the dict produced by build_report_data.
    `errors` maps dataset name -> failure message for tables that could not
    be audited at all.
    """
    per_table = []
    scores, total_rules, total_failed, total_anoms = [], 0, 0, 0
    for ds, a in audits.items():
        run = a.get("run") or {}
        score = run.get("score")
        if isinstance(score, (int, float)):
            scores.append(score)
        total_rules += run.get("total_rules") or 0
        total_failed += run.get("failed_rules") or 0
        total_anoms += (a.get("anomalies") or {}).get("count") or 0
        failed = [r for r in (run.get("results") or []) if not r.get("passed")]
        per_table.append({
            "dataset": ds,
            "score": score,
            "rows": (a.get("profile") or {}).get("row_count"),
            "columns": (a.get("profile") or {}).get("column_count"),
            "rules_total": run.get("total_rules", 0),
            "rules_failed": run.get("failed_rules", 0),
            "anomalies": (a.get("anomalies") or {}).get("count", 0),
            "severity": (a.get("analysis") or {}).get("overall_severity"),
            "failed_rules": [
                {"rule": r.get("rule"), "column": r.get("column"),
                 "severity": r.get("severity"), "pass_rate": r.get("pass_rate"),
                 "rows_failed": r.get("rows_failed")}
                for r in failed[:10]
            ],
        })
    per_table.sort(key=lambda t: (t["score"] is None, t["score"] or 0))

    return {
        "title": "DataVigil Schema Audit Report",
        "dataset": f"{source} — all tables" + (f" ({schema})" if schema else ""),
        "source": source,
        "schema": schema,
        "generated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "is_schema_audit": True,
        "summary": {
            "tables_found": len(audits) + len(errors or {}),
            "tables_audited": len(audits),
            "tables_failed": len(errors or {}),
            "avg_score": round(sum(scores) / len(scores), 1) if scores else None,
            "min_score": min(scores) if scores else None,
            "total_rules": total_rules,
            "total_failed_rules": total_failed,
            "total_anomalies": total_anoms,
        },
        "tables": per_table,
        "audits": audits,            # full per-table bundles for the renderers
        "errors": [{"dataset": d, "error": e} for d, e in (errors or {}).items()],
    }
