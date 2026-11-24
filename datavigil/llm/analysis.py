"""AI root-cause analysis for a rule run.

Sends the failed rules (with pass rates + example bad values) and a compact
column profile to the LLM and returns a structured assessment — severity,
likely root cause, and a concrete recommended fix per issue — the same
information the commercial DataVigil-style tools surface to non-technical users.

Output schema (all fields optional except the envelope):
{
  "summary": "one-paragraph plain-language overview",
  "overall_severity": "info|warning|error|critical",
  "issues": [
    {"rule": "<rule name>", "column": "<col>", "root_cause": "...",
     "impact": "...", "recommended_fix": "...", "priority": "high|medium|low"}
  ]
}
"""
from __future__ import annotations

import json
from typing import Optional

from ..core.models import RunResult, TableProfile, to_jsonable
from .provider import LLMProvider, LLMUnavailable, extract_json

ANALYSIS_SYSTEM = """You are a senior data quality engineer writing for a
non-technical audience. You are given the failed data quality checks from a
run, including pass rates and real examples of the bad values, plus a compact
column profile.

Produce STRICT JSON with this shape:
{
  "summary": "1-3 sentence plain-language overview of what is wrong",
  "overall_severity": "info | warning | error | critical",
  "issues": [
    {
      "rule": "<the rule name from the input>",
      "column": "<column or null>",
      "root_cause": "<most likely reason this data is bad>",
      "impact": "<what breaks downstream if not fixed>",
      "recommended_fix": "<concrete, actionable fix>",
      "priority": "high | medium | low"
    }
  ]
}

Rules:
- One entry in "issues" per failed rule, worst first.
- Use ONLY the data provided; do not invent columns or values.
- Keep each field to one sentence where possible.
- Output ONLY the JSON object."""


def _failed_payload(run: RunResult) -> list[dict]:
    failed = [r for r in run.results if not r.passed]
    return [
        {
            "rule": r.rule_name,
            "rule_type": r.rule_type,
            "column": r.column,
            "dimension": r.dimension.value if hasattr(r.dimension, "value") else r.dimension,
            "severity": r.severity.value if hasattr(r.severity, "value") else r.severity,
            "pass_rate": r.pass_rate,
            "threshold": r.threshold,
            "rows_failed": r.rows_failed,
            "rows_evaluated": r.rows_evaluated,
            "message": r.message,
            "example_bad_values": r.failed_examples[:5],
        }
        for r in failed
    ]


def analyze_run(run: RunResult, llm: LLMProvider,
                profile: Optional[TableProfile] = None) -> dict:
    """LLM root-cause analysis of a run's failed rules.

    Returns a dict with summary / overall_severity / issues. Falls back to a
    deterministic (non-LLM) analysis when the LLM is unavailable or fails, so
    the report and UI always have something useful to show.
    """
    failed = _failed_payload(run)
    if not failed:
        return {
            "summary": "All data quality checks passed — no issues to analyse.",
            "overall_severity": "info",
            "issues": [],
        }

    if not llm.available:
        return _fallback(run, failed,
                         note="LLM unavailable — heuristic analysis only.")

    context: dict = {
        "dataset": run.dataset,
        "dq_score": run.score,
        "dimension_scores": run.dimension_scores,
        "failed_rules": failed,
    }
    if profile:
        context["columns"] = [
            {"name": c.name, "type": c.inferred_type,
             "null_pct": c.null_pct, "distinct": c.distinct_count,
             "min": c.min, "max": c.max,
             "top_values": c.top_values[:5]}
            for c in profile.columns
        ]
    user = json.dumps(to_jsonable(context), default=str)[:14000]

    try:
        raw = llm.complete(ANALYSIS_SYSTEM, user, json_mode=True)
        data = extract_json(raw)
    except Exception as e:
        return _fallback(run, failed, note=f"LLM analysis failed ({e}) — "
                                           "heuristic analysis only.")
    if not isinstance(data, dict) or "issues" not in data:
        return _fallback(run, failed,
                         note="LLM returned unusable output — heuristic analysis only.")

    data.setdefault("summary", "")
    data.setdefault("overall_severity", "warning")
    data["generated_by"] = "llm"
    return data


def _fallback(run: RunResult, failed: list[dict], note: str = "") -> dict:
    """Deterministic analysis used when the LLM can't help."""
    sev_rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
    worst = max((sev_rank.get(f["severity"], 1) for f in failed), default=1)
    overall = {0: "info", 1: "warning", 2: "error", 3: "critical"}[worst]
    issues = [
        {
            "rule": f["rule"],
            "column": f["column"],
            "root_cause": (
                f"{f['rows_failed']} of {f['rows_evaluated']} rows "
                f"({round(100 * (1 - f['pass_rate']), 1)}%) violate "
                f"'{f['rule_type']}'."
            ),
            "impact": f"Data failing {f['dimension']} checks may be unreliable "
                      "for reporting and downstream use.",
            "recommended_fix": "Inspect the example failing values and correct "
                               "at source, or adjust the rule threshold if the "
                               "values are acceptable.",
            "priority": "high" if f["severity"] in ("error", "critical") else "medium",
        }
        for f in failed
    ]
    summary = (f"{len(failed)} of {run.total_rules} checks failed on "
               f"'{run.dataset}' (DQ score {run.score}/100).")
    if note:
        summary += " " + note
    return {
        "summary": summary,
        "overall_severity": overall,
        "issues": issues,
        "generated_by": "heuristic",
    }
