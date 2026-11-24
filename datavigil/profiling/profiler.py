"""The profiling engine.

Given a DataFrame, produces a TableProfile with per-column statistics,
pattern detection, value distributions, and cross-column correlation.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ..core.config import Config
from ..core.models import (
    ColumnProfile,
    CorrelationPair,
    TableProfile,
)
from .patterns import infer_patterns, dominant_named_pattern


def _infer_logical_type(series: pd.Series) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"
    if pd.api.types.is_numeric_dtype(series):
        return "numeric"
    if pd.api.types.is_datetime64_any_dtype(series):
        return "datetime"
    # try datetime parse on object / string columns (pandas 2.x may use a
    # dedicated string dtype rather than object)
    if series.dtype == object or pd.api.types.is_string_dtype(series):
        non_null = series.dropna()
        if len(non_null) == 0:
            return "string"
        sample = non_null.head(200).astype(str)
        parsed = pd.to_datetime(sample, errors="coerce", format="mixed")
        if parsed.notna().mean() >= 0.9:
            return "datetime"
        # categorical heuristic: low cardinality
        if non_null.nunique() <= max(20, 0.05 * len(non_null)):
            return "categorical"
    return "string"


def _top_values(series: pd.Series, n: int) -> list[dict]:
    vc = series.value_counts(dropna=True).head(n)
    total = len(series)
    out = []
    for value, count in vc.items():
        out.append({
            "value": _safe(value),
            "count": int(count),
            "pct": round(100 * count / total, 2) if total else 0.0,
        })
    return out


def _safe(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if isinstance(v, (np.bool_,)):
        return bool(v)
    if pd.isna(v):
        return None
    return v


def profile_column(series: pd.Series, cfg: Config) -> ColumnProfile:
    name = str(series.name)
    count = int(len(series))
    null_count = int(series.isna().sum())
    non_null = series.dropna()
    distinct = int(non_null.nunique())
    logical = _infer_logical_type(series)

    prof = ColumnProfile(
        name=name,
        inferred_type=logical,
        dtype=str(series.dtype),
        count=count,
        null_count=null_count,
        null_pct=round(100 * null_count / count, 2) if count else 0.0,
        distinct_count=distinct,
        distinct_pct=round(100 * distinct / count, 2) if count else 0.0,
        is_unique=(distinct == count - null_count and count > 0 and null_count == 0),
    )

    if logical == "numeric" and len(non_null) > 0:
        num = pd.to_numeric(non_null, errors="coerce").dropna()
        if len(num) > 0:
            prof.min = float(num.min())
            prof.max = float(num.max())
            prof.mean = float(num.mean())
            prof.median = float(num.median())
            prof.std = float(num.std()) if len(num) > 1 else 0.0
            prof.q25 = float(num.quantile(0.25))
            prof.q75 = float(num.quantile(0.75))
            prof.zeros = int((num == 0).sum())
            prof.negatives = int((num < 0).sum())

    elif logical == "datetime" and len(non_null) > 0:
        dt = pd.to_datetime(non_null, errors="coerce", format="mixed").dropna()
        if len(dt) > 0:
            prof.min_date = dt.min().isoformat()
            prof.max_date = dt.max().isoformat()

    else:  # string / categorical / boolean
        as_str = non_null.astype(str)
        if len(as_str) > 0:
            lengths = as_str.str.len()
            prof.min_length = int(lengths.min())
            prof.max_length = int(lengths.max())
            prof.mean_length = round(float(lengths.mean()), 2)
            prof.blank_count = int((as_str.str.strip() == "").sum())
            # pattern inference on a sample
            sample = as_str.head(cfg.pattern_sample).tolist()
            prof.patterns = infer_patterns(sample)

    prof.top_values = _top_values(non_null, cfg.max_distinct_tracked)
    prof.sample_values = [_safe(v) for v in non_null.head(5).tolist()]
    return prof


def compute_correlations(df: pd.DataFrame, cfg: Config) -> list[CorrelationPair]:
    numeric = df.select_dtypes(include=[np.number])
    if numeric.shape[1] < 2:
        return []
    corr = numeric.corr(method="pearson", numeric_only=True)
    pairs: list[CorrelationPair] = []
    cols = list(corr.columns)
    for i in range(len(cols)):
        for j in range(i + 1, len(cols)):
            c = corr.iloc[i, j]
            if pd.notna(c) and abs(c) >= cfg.correlation_threshold:
                pairs.append(CorrelationPair(
                    col_a=cols[i], col_b=cols[j],
                    coefficient=round(float(c), 4), method="pearson",
                ))
    pairs.sort(key=lambda p: abs(p.coefficient), reverse=True)
    return pairs


def profile_dataframe(df: pd.DataFrame, dataset: str, cfg: Optional[Config] = None) -> TableProfile:
    cfg = cfg or Config.load()
    if len(df) > cfg.max_profile_sample:
        df = df.sample(cfg.max_profile_sample, random_state=42)

    columns = [profile_column(df[col], cfg) for col in df.columns]
    dup_count = int(df.duplicated().sum())

    return TableProfile(
        dataset=dataset,
        row_count=int(len(df)),
        column_count=int(df.shape[1]),
        duplicate_row_count=dup_count,
        columns=columns,
        correlations=compute_correlations(df, cfg),
    )


def compare_tables(prof_a: TableProfile, prof_b: TableProfile) -> dict:
    """Multi-table comparison: schema drift + shared-column stat shifts."""
    cols_a = {c.name: c for c in prof_a.columns}
    cols_b = {c.name: c for c in prof_b.columns}
    shared = sorted(set(cols_a) & set(cols_b))

    column_diffs = []
    for name in shared:
        a, b = cols_a[name], cols_b[name]
        column_diffs.append({
            "column": name,
            "type_a": a.inferred_type,
            "type_b": b.inferred_type,
            "type_changed": a.inferred_type != b.inferred_type,
            "null_pct_a": a.null_pct,
            "null_pct_b": b.null_pct,
            "null_pct_delta": round(b.null_pct - a.null_pct, 2),
            "distinct_a": a.distinct_count,
            "distinct_b": b.distinct_count,
        })

    return {
        "dataset_a": prof_a.dataset,
        "dataset_b": prof_b.dataset,
        "row_count_a": prof_a.row_count,
        "row_count_b": prof_b.row_count,
        "row_count_delta": prof_b.row_count - prof_a.row_count,
        "only_in_a": sorted(set(cols_a) - set(cols_b)),
        "only_in_b": sorted(set(cols_b) - set(cols_a)),
        "shared_columns": shared,
        "column_diffs": column_diffs,
    }
