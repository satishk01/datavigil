"""Anomaly detection: statistical + ML.

Methods:
  - zscore        : univariate numeric outliers (|z| > k)
  - iqr           : univariate numeric outliers (Tukey fences)
  - isolation     : multivariate ML outliers across all numeric columns
  - rare_category : low-frequency categorical values
  - pattern_break : string values that don't match the dominant pattern

detect() runs an appropriate subset and returns a unified AnomalyResult.
A real-time single-row scorer is provided for streaming use.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd
from sklearn.ensemble import IsolationForest
from sklearn.preprocessing import StandardScaler

from ..core.config import Config
from ..core.models import Anomaly, AnomalyResult, Severity
from ..profiling.patterns import structural_mask, dominant_named_pattern
import re


def _numeric_cols(df: pd.DataFrame) -> list[str]:
    return list(df.select_dtypes(include=[np.number]).columns)


def zscore_anomalies(df: pd.DataFrame, k: float = 3.0, max_per_col: int = 50) -> list[Anomaly]:
    out: list[Anomaly] = []
    for col in _numeric_cols(df):
        s = df[col].dropna()
        if len(s) < 10 or s.std() == 0:
            continue
        z = (s - s.mean()) / s.std()
        flagged = z[abs(z) > k]
        for idx, zval in flagged.head(max_per_col).items():
            out.append(Anomaly(
                column=col, method="zscore", row_index=int(idx),
                value=float(df.at[idx, col]), score=round(abs(float(zval)), 3),
                reason=f"{abs(zval):.1f} std devs from mean ({s.mean():.2f})",
                severity=Severity.WARNING if abs(zval) < 5 else Severity.ERROR,
            ))
    return out


def iqr_anomalies(df: pd.DataFrame, factor: float = 1.5, max_per_col: int = 50) -> list[Anomaly]:
    out: list[Anomaly] = []
    for col in _numeric_cols(df):
        s = df[col].dropna()
        if len(s) < 10:
            continue
        q1, q3 = s.quantile(0.25), s.quantile(0.75)
        iqr = q3 - q1
        if iqr == 0:
            continue
        lo, hi = q1 - factor * iqr, q3 + factor * iqr
        flagged = s[(s < lo) | (s > hi)]
        for idx, val in flagged.head(max_per_col).items():
            dist = (lo - val) if val < lo else (val - hi)
            out.append(Anomaly(
                column=col, method="iqr", row_index=int(idx), value=float(val),
                score=round(float(dist / iqr), 3),
                reason=f"outside Tukey fence [{lo:.2f}, {hi:.2f}]",
            ))
    return out


def isolation_forest_anomalies(df: pd.DataFrame, contamination: float = 0.02,
                               max_report: int = 100) -> list[Anomaly]:
    num = df[_numeric_cols(df)].dropna()
    if num.shape[0] < 20 or num.shape[1] < 1:
        return []
    X = StandardScaler().fit_transform(num.values)
    model = IsolationForest(
        contamination=min(max(contamination, 0.001), 0.5),
        random_state=42, n_estimators=200,
    )
    preds = model.fit_predict(X)
    scores = model.score_samples(X)   # lower = more anomalous
    out: list[Anomaly] = []
    flagged_idx = num.index[preds == -1]
    ranked = sorted(flagged_idx, key=lambda i: scores[num.index.get_loc(i)])
    for idx in ranked[:max_report]:
        pos = num.index.get_loc(idx)
        row = {c: _safe(num.at[idx, c]) for c in num.columns}
        out.append(Anomaly(
            column=None, method="isolation_forest", row_index=int(idx),
            value=row, score=round(float(-scores[pos]), 4),
            reason="multivariate outlier across numeric columns",
        ))
    return out


def rare_category_anomalies(df: pd.DataFrame, min_pct: float = 0.5,
                            max_per_col: int = 30) -> list[Anomaly]:
    out: list[Anomaly] = []
    n = len(df)
    for col in df.select_dtypes(include=["object", "string", "category"]).columns:
        s = df[col].dropna().astype(str)
        if len(s) < 20:
            continue
        distinct = s.nunique()
        if distinct > max(50, 0.5 * len(s)):   # free-text, skip
            continue
        freq = s.value_counts(normalize=True) * 100
        rare = freq[freq < min_pct]
        for val, pct in rare.head(max_per_col).items():
            out.append(Anomaly(
                column=col, method="rare_category", row_index=None, value=val,
                score=round(float(min_pct - pct), 3),
                reason=f"value appears in only {pct:.2f}% of rows",
                severity=Severity.INFO,
            ))
    return out


def pattern_break_anomalies(df: pd.DataFrame, max_per_col: int = 30) -> list[Anomaly]:
    out: list[Anomaly] = []
    for col in df.select_dtypes(include=["object", "string"]).columns:
        s = df[col].dropna().astype(str)
        if len(s) < 20:
            continue
        sample = s.head(10_000).tolist()
        named = dominant_named_pattern(sample, threshold=0.9)
        if named:
            from ..profiling.patterns import NAMED_PATTERNS
            rx = dict((n, r) for n, r in NAMED_PATTERNS)[named]
            broken = s[~s.map(lambda v: bool(rx.match(v)))]
            for idx, val in broken.head(max_per_col).items():
                out.append(Anomaly(
                    column=col, method="pattern_break", row_index=int(idx),
                    value=val, score=1.0,
                    reason=f"does not match dominant pattern '{named}'",
                    severity=Severity.ERROR,
                ))
        else:
            masks = s.map(structural_mask)
            top = masks.value_counts(normalize=True)
            if len(top) and top.iloc[0] >= 0.8:
                dominant = top.index[0]
                broken = s[masks != dominant]
                for idx, val in broken.head(max_per_col).items():
                    out.append(Anomaly(
                        column=col, method="pattern_break", row_index=int(idx),
                        value=val, score=round(float(top.iloc[0]), 3),
                        reason=f"shape differs from dominant mask '{dominant}'",
                        severity=Severity.WARNING,
                    ))
    return out


def _safe(v):
    if isinstance(v, (np.integer,)):
        return int(v)
    if isinstance(v, (np.floating,)):
        return float(v)
    if pd.isna(v):
        return None
    return v


def detect(df: pd.DataFrame, dataset: str, methods: Optional[list[str]] = None,
           cfg: Optional[Config] = None) -> AnomalyResult:
    cfg = cfg or Config.load()
    methods = methods or ["iqr", "isolation", "rare_category", "pattern_break"]
    anomalies: list[Anomaly] = []
    used: list[str] = []

    if "zscore" in methods:
        anomalies += zscore_anomalies(df); used.append("zscore")
    if "iqr" in methods:
        anomalies += iqr_anomalies(df); used.append("iqr")
    if "isolation" in methods:
        anomalies += isolation_forest_anomalies(df, cfg.anomaly_contamination)
        used.append("isolation_forest")
    if "rare_category" in methods:
        anomalies += rare_category_anomalies(df); used.append("rare_category")
    if "pattern_break" in methods:
        anomalies += pattern_break_anomalies(df); used.append("pattern_break")

    anomalies.sort(key=lambda a: a.score, reverse=True)
    return AnomalyResult(
        dataset=dataset, anomalies=anomalies, total_rows=int(len(df)),
        anomaly_count=len(anomalies), methods_used=used,
    )


# --------------------------------------------------------------------------
# Real-time / streaming scorer
# --------------------------------------------------------------------------
class RealtimeScorer:
    """Fit a model on a training DataFrame, then score incoming rows one at a
    time (e.g. as data flows in). Flags rows the trained IsolationForest and
    the fitted per-column stats consider anomalous."""

    def __init__(self, train_df: pd.DataFrame, contamination: float = 0.02):
        self.numeric = _numeric_cols(train_df)
        self._stats = {}
        for c in self.numeric:
            s = train_df[c].dropna()
            self._stats[c] = (float(s.mean()), float(s.std() or 1.0))
        self._scaler = None
        self._model = None
        clean = train_df[self.numeric].dropna()
        if clean.shape[0] >= 20 and clean.shape[1] >= 1:
            self._scaler = StandardScaler().fit(clean.values)
            self._model = IsolationForest(
                contamination=min(max(contamination, 0.001), 0.5),
                random_state=42, n_estimators=200,
            ).fit(self._scaler.transform(clean.values))

    def score_row(self, row: dict) -> dict:
        reasons = []
        for c in self.numeric:
            if c in row and row[c] is not None:
                mean, std = self._stats[c]
                try:
                    z = abs((float(row[c]) - mean) / std)
                    if z > 3:
                        reasons.append(f"{c}: {z:.1f} std devs from mean")
                except (TypeError, ValueError):
                    pass
        ml_flag = False
        ml_score = None
        if self._model is not None and all(
            c in row and row[c] is not None for c in self.numeric
        ):
            try:
                X = self._scaler.transform([[float(row[c]) for c in self.numeric]])
                ml_flag = self._model.predict(X)[0] == -1
                ml_score = round(float(-self._model.score_samples(X)[0]), 4)
            except (TypeError, ValueError):
                pass
        is_anomaly = bool(reasons) or bool(ml_flag)
        return {
            "is_anomaly": bool(is_anomaly),
            "ml_flag": bool(ml_flag),
            "ml_score": ml_score,
            "reasons": reasons,
        }
