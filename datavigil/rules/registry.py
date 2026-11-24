"""Rule type registry.

Each rule type is a function that evaluates a DataFrame and returns a
(rows_evaluated, failed_mask, message) tuple. The engine turns that into a
RuleResult with pass-rate, examples, and scoring.

New rule types register via the @rule decorator, so NL-generated rules and
custom client rules plug in without touching the engine.
"""
from __future__ import annotations

import re
from typing import Callable, Optional

import numpy as np
import pandas as pd

# signature: (df, column, params) -> (failed_mask: pd.Series[bool], message: str)
RuleFn = Callable[[pd.DataFrame, Optional[str], dict], tuple[pd.Series, str]]

_REGISTRY: dict[str, dict] = {}


def rule(name: str, *, dimension: str, label: str, needs_column: bool = True,
         param_schema: Optional[dict] = None):
    def deco(fn: RuleFn):
        _REGISTRY[name] = {
            "fn": fn,
            "dimension": dimension,
            "label": label,
            "needs_column": needs_column,
            "param_schema": param_schema or {},
        }
        return fn
    return deco


def get_rule(name: str) -> dict:
    if name not in _REGISTRY:
        raise KeyError(f"Unknown rule type: {name}")
    return _REGISTRY[name]


def list_rule_types() -> list[dict]:
    return [
        {"type": k, "dimension": v["dimension"], "label": v["label"],
         "needs_column": v["needs_column"], "params": v["param_schema"]}
        for k, v in sorted(_REGISTRY.items())
    ]


def _col(df: pd.DataFrame, column: Optional[str]) -> pd.Series:
    if column is None or column not in df.columns:
        raise KeyError(f"Column not found: {column}")
    return df[column]


# --------------------------------------------------------------------------
# Completeness
# --------------------------------------------------------------------------
@rule("not_null", dimension="completeness", label="Column has no nulls")
def _not_null(df, column, params):
    s = _col(df, column)
    return s.isna(), f"'{column}' must not be null"


@rule("not_blank", dimension="completeness", label="String column has no blanks/whitespace")
def _not_blank(df, column, params):
    s = _col(df, column).astype("string")
    failed = s.isna() | (s.str.strip() == "")
    return failed.fillna(True), f"'{column}' must not be blank"


@rule("completeness_pct", dimension="completeness",
      label="At least X% of rows are populated", param_schema={"min_pct": 95})
def _completeness_pct(df, column, params):
    # threshold handled by engine; here every null is a failure
    s = _col(df, column)
    return s.isna(), f"'{column}' completeness check"


# --------------------------------------------------------------------------
# Uniqueness
# --------------------------------------------------------------------------
@rule("unique", dimension="uniqueness", label="Column values are unique")
def _unique(df, column, params):
    s = _col(df, column)
    failed = s.duplicated(keep=False) & s.notna()
    return failed, f"'{column}' must be unique"


@rule("unique_combination", dimension="uniqueness",
      label="Combination of columns is unique", needs_column=False,
      param_schema={"columns": ["col1", "col2"]})
def _unique_combo(df, column, params):
    cols = params.get("columns", [])
    failed = df.duplicated(subset=cols, keep=False)
    return failed, f"combination {cols} must be unique"


# --------------------------------------------------------------------------
# Validity
# --------------------------------------------------------------------------
@rule("range", dimension="validity", label="Numeric value within [min, max]",
      param_schema={"min": 0, "max": 100})
def _range(df, column, params):
    s = pd.to_numeric(_col(df, column), errors="coerce")
    lo = params.get("min")
    hi = params.get("max")
    failed = pd.Series(False, index=s.index)
    if lo is not None:
        failed |= s < lo
    if hi is not None:
        failed |= s > hi
    failed |= s.isna() & _col(df, column).notna()   # non-numeric where numeric expected
    return failed.fillna(False), f"'{column}' must be within [{lo}, {hi}]"


@rule("regex", dimension="validity", label="Value matches a regex",
      param_schema={"pattern": "^[A-Z]{3}$"})
def _regex(df, column, params):
    s = _col(df, column)
    rx = re.compile(params["pattern"])
    as_str = s.astype("string")
    # nulls are a completeness concern, not validity — they pass here
    failed = s.notna() & ~as_str.fillna("").map(lambda v: bool(rx.match(v)))
    return failed, f"'{column}' must match /{params['pattern']}/"


@rule("in_set", dimension="validity", label="Value is in an allowed set",
      param_schema={"values": ["A", "B", "C"]})
def _in_set(df, column, params):
    s = _col(df, column)
    allowed = set(params.get("values", []))
    failed = ~s.isin(allowed) & s.notna()
    return failed, f"'{column}' must be one of {sorted(allowed)}"


@rule("length", dimension="validity", label="String length within bounds",
      param_schema={"min": 1, "max": 50})
def _length(df, column, params):
    raw = _col(df, column)
    s = raw.astype("string")
    lo = params.get("min", 0)
    hi = params.get("max")
    lengths = s.str.len()
    failed = raw.notna() & (lengths < lo)
    if hi is not None:
        failed |= raw.notna() & (lengths > hi)
    return failed.fillna(False), f"'{column}' length must be within [{lo}, {hi}]"


@rule("dtype", dimension="validity", label="Values parse as a given type",
      param_schema={"type": "integer"})
def _dtype(df, column, params):
    s = _col(df, column)
    want = params.get("type", "integer")
    non_null = s.notna()
    if want in ("integer", "int"):
        coerced = pd.to_numeric(s, errors="coerce")
        failed = (coerced.isna() & non_null) | ((coerced % 1 != 0) & coerced.notna())
    elif want in ("number", "float", "decimal"):
        failed = pd.to_numeric(s, errors="coerce").isna() & non_null
    elif want in ("date", "datetime"):
        failed = pd.to_datetime(s, errors="coerce", format="mixed").isna() & non_null
    elif want in ("boolean", "bool"):
        failed = ~s.astype("string").str.lower().isin(
            ["true", "false", "0", "1", "yes", "no", "y", "n"]) & non_null
    else:
        failed = pd.Series(False, index=s.index)
    return failed.fillna(False), f"'{column}' must be of type {want}"


# --------------------------------------------------------------------------
# Consistency / Accuracy
# --------------------------------------------------------------------------
@rule("expression", dimension="consistency",
      label="Rows satisfy a boolean expression", needs_column=False,
      param_schema={"expr": "price >= cost"})
def _expression(df, column, params):
    expr = params["expr"]
    try:
        passed = df.eval(expr)
    except Exception as e:
        raise ValueError(f"Invalid expression '{expr}': {e}")
    if passed.dtype != bool:
        passed = passed.astype(bool)
    return ~passed, f"rows must satisfy: {expr}"


@rule("freshness", dimension="timeliness",
      label="Datetime column is recent enough",
      param_schema={"max_age_days": 30})
def _freshness(df, column, params):
    s = pd.to_datetime(_col(df, column), errors="coerce", format="mixed", utc=True)
    max_age = params.get("max_age_days", 30)
    cutoff = pd.Timestamp.now(tz="UTC") - pd.Timedelta(days=max_age)
    failed = (s < cutoff) | s.isna()
    return failed.fillna(True), f"'{column}' must be within {max_age} days"
