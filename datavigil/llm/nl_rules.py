"""Natural-language → data quality rules, and plain-language insights.

The LLM is constrained to the rule-type registry: it must emit JSON that maps
onto known rule types, which we then validate before constructing Rule objects.
Invalid output is rejected rather than executed, so a hallucinated rule type
can never run.
"""
from __future__ import annotations

import json
from typing import Optional

from ..core.models import DQDimension, Rule, Severity, TableProfile
from ..rules.registry import list_rule_types, get_rule
from .provider import LLMProvider, LLMUnavailable, extract_json


def _rule_catalog() -> str:
    lines = []
    for rt in list_rule_types():
        params = json.dumps(rt["params"]) if rt["params"] else "{}"
        lines.append(
            f"- {rt['type']} (dimension={rt['dimension']}, "
            f"needs_column={rt['needs_column']}, example_params={params}): {rt['label']}"
        )
    return "\n".join(lines)


NL_RULES_SYSTEM = """You are a data quality rule generator for DataVigil.
Translate the user's request into one or more rules drawn ONLY from the
available rule types below. Output STRICT JSON: a list of rule objects.

Each rule object has:
  name        : short human label
  rule_type   : MUST be one of the available types
  column      : the target column name, or null for table-level rules
  params      : object of parameters for that rule type (use the example shape)
  dimension   : one of completeness, uniqueness, validity, consistency, accuracy, timeliness, integrity
  severity    : one of info, warning, error, critical
  threshold   : fraction 0.0-1.0 of rows that must pass (default 1.0)

Available rule types:
{catalog}

Available columns in the dataset:
{columns}

Rules:
- Use ONLY listed rule types and listed column names.
- If the request is ambiguous, make the most reasonable choice.
- Output ONLY the JSON list, no prose."""


def nl_to_rules(request: str, llm: LLMProvider,
                profile: Optional[TableProfile] = None,
                columns: Optional[list[str]] = None) -> list[Rule]:
    if not llm.available:
        raise LLMUnavailable("LLM is not available; cannot translate NL to rules.")

    col_names = columns or ([c.name for c in profile.columns] if profile else [])
    system = NL_RULES_SYSTEM.format(
        catalog=_rule_catalog(),
        columns=", ".join(col_names) if col_names else "(unknown — infer from request)",
    )
    raw = llm.complete(system, request, json_mode=True)
    data = extract_json(raw)
    if data is None:
        raise ValueError(f"Model did not return valid JSON. Got: {raw[:200]}")
    if isinstance(data, dict):
        data = data.get("rules", [data])

    rules: list[Rule] = []
    errors: list[str] = []
    for item in data:
        try:
            rt = item["rule_type"]
            spec = get_rule(rt)  # validates the type exists
            col = item.get("column")
            if spec["needs_column"] and not col:
                errors.append(f"{rt}: requires a column")
                continue
            if col and col_names and col not in col_names:
                errors.append(f"{rt}: unknown column '{col}'")
                continue
            rules.append(Rule(
                name=item.get("name", f"{rt} on {col}"),
                rule_type=rt,
                column=col,
                params=item.get("params", {}) or {},
                dimension=DQDimension(item.get("dimension", spec["dimension"])),
                severity=Severity(item.get("severity", "error")),
                threshold=float(item.get("threshold", 1.0)),
                description=request,
                source="nl",
            ))
        except (KeyError, ValueError) as e:
            errors.append(str(e))

    if not rules and errors:
        raise ValueError("Could not build any valid rule: " + "; ".join(errors))
    return rules


# --------------------------------------------------------------------------
# Plain-language profile insights
# --------------------------------------------------------------------------
INSIGHT_SYSTEM = """You are a senior data quality analyst. Given a JSON data
profile, write a concise plain-language assessment for a business stakeholder.
Cover: overall health, the most important completeness/uniqueness/validity
concerns, any suspicious columns, and 2-4 concrete recommended checks.
Be specific and reference real column names and numbers. Use short paragraphs
or bullets. Do not invent data not present in the profile."""


def profile_insights(profile: TableProfile, llm: LLMProvider) -> str:
    if not llm.available:
        raise LLMUnavailable("LLM is not available; cannot generate insights.")
    from ..core.models import to_jsonable
    # trim payload: drop verbose sample_values for the prompt
    compact = to_jsonable(profile)
    for c in compact.get("columns", []):
        c.pop("sample_values", None)
    user = json.dumps(compact)[:12000]
    return llm.complete(INSIGHT_SYSTEM, user)


# --------------------------------------------------------------------------
# Conversational query over a run result
# --------------------------------------------------------------------------
ASK_SYSTEM = """You are DataVigil's assistant. Answer the user's question using
ONLY the provided JSON context (profile, rule run, anomalies). If the answer
isn't in the context, say so. Be concise and cite specific columns/numbers."""


def ask(question: str, context: dict, llm: LLMProvider) -> str:
    if not llm.available:
        raise LLMUnavailable("LLM is not available.")
    user = f"CONTEXT:\n{json.dumps(context)[:12000]}\n\nQUESTION: {question}"
    return llm.complete(ASK_SYSTEM, user)
