"""Source -> target reconciliation engine.

Given a DatasetMapping, loads both sides, applies derived-column transforms
and renames on the source, then runs a battery of reconciliation checks:

  schema_presence     every mapped target column exists on both sides
  row_count           source vs target row counts (strict for 1:1)
  key_uniqueness      key uniqueness required by the declared cardinality
  missing_in_target   source keys with no target row
  orphans_in_target   target keys with no source row
  value_match         row-level comparison of mapped columns on joined keys
  aggregate_match     count/sum/min/max/mean parity between the two sides

Every check reports pass/fail plus a bounded set of example rows, mirroring
the rule engine's style so results slot into reports and the UI cleanly.
"""
from __future__ import annotations

from typing import Optional

import numpy as np
import pandas as pd

from ..core.models import Cardinality, DatasetMapping

_MAX_EXAMPLES = 10


def _eval_transform(df: pd.DataFrame, expr: str):
    """Evaluate a transform expression on the source frame.

    Tries pandas' fast evaluator first; falls back to a plain eval with the
    columns bound as Series, which also supports expressions pandas can't
    parse (e.g. string concat `first + ' ' + last` in some versions).
    """
    try:
        return df.eval(expr)
    except Exception:
        env = {c: df[c] for c in df.columns}
        env["np"] = np
        env["pd"] = pd
        safe_builtins = {"str": str, "int": int, "float": float, "abs": abs,
                         "round": round, "len": len, "min": min, "max": max,
                         "bool": bool}
        return eval(expr, {"__builtins__": safe_builtins}, env)  # noqa: S307


def apply_mapping(df_src: pd.DataFrame,
                  mapping: DatasetMapping) -> tuple[pd.DataFrame, list[str]]:
    """Apply derived-column transforms + src->tgt renames to the source frame.

    Returns (mapped_df, errors). Column names produced on the mapped frame use
    the TARGET names so the two frames can be compared directly.
    """
    df = df_src.copy()
    errors: list[str] = []
    renames: dict[str, str] = {}
    for cm in mapping.columns:
        if cm.transform:
            try:
                df[cm.tgt] = _eval_transform(df, cm.transform)
            except Exception as e:
                errors.append(f"transform for '{cm.tgt}' failed: {e}")
        elif cm.src:
            if cm.src not in df.columns:
                errors.append(f"source column '{cm.src}' not found")
            elif cm.src != cm.tgt:
                renames[cm.src] = cm.tgt
    # key renames too — keys may be renamed even if not in `columns`
    for kp in mapping.key_pairs:
        src, tgt = kp.get("src"), kp.get("tgt")
        if src and tgt and src != tgt:
            if src in df.columns:
                renames[src] = tgt
            else:
                errors.append(f"key column '{src}' not found in source")
    if renames:
        overlap = set(renames.values()) & (set(df.columns) - set(renames))
        if overlap:
            errors.append(f"rename collision on target column(s): {sorted(overlap)}")
        df = df.rename(columns=renames)
    return df, errors


def _key_tuples(df: pd.DataFrame, cols: list[str]) -> pd.Series:
    if len(cols) == 1:
        return df[cols[0]]
    return df[cols].astype(str).agg("\x1f".join, axis=1)


def _examples(series_vals, n=_MAX_EXAMPLES) -> list:
    out = []
    for v in list(series_vals)[:n]:
        try:
            v = v.item() if hasattr(v, "item") else v
        except Exception:
            pass
        out.append(v if v is None or isinstance(v, (int, float, str, bool))
                  else str(v))
    return out


def reconcile(df_src: pd.DataFrame, df_tgt: pd.DataFrame,
              mapping: DatasetMapping) -> dict:
    checks: list[dict] = []

    def check(name: str, passed: bool, detail: str,
              severity: str = "error", examples: Optional[list] = None):
        checks.append({"name": name, "passed": bool(passed),
                       "severity": severity, "detail": detail,
                       "examples": examples or []})

    src_key_cols = [k["src"] for k in mapping.key_pairs]
    tgt_key_cols = [k.get("tgt") or k["src"] for k in mapping.key_pairs]

    df_a, map_errors = apply_mapping(df_src, mapping)
    for e in map_errors:
        check("mapping", False, e, severity="critical")
    df_b = df_tgt

    # -- schema presence ---------------------------------------------------
    missing_src = [c for c in src_key_cols + [cm.src for cm in mapping.columns
                                            if cm.src and not cm.transform]
                   if c and c not in df_src.columns]
    needed_tgt = set(tgt_key_cols) | {cm.tgt for cm in mapping.columns}
    missing_tgt = [c for c in sorted(needed_tgt) if c not in df_b.columns]
    unmapped_tgt = [c for c in df_b.columns if c not in needed_tgt]
    detail = []
    if missing_src:
        detail.append(f"missing in source: {missing_src}")
    if missing_tgt:
        detail.append(f"missing in target: {missing_tgt}")
    if unmapped_tgt:
        detail.append(f"unmapped target columns (not checked): {unmapped_tgt}")
    check("schema_presence", not missing_src and not missing_tgt,
          "; ".join(detail) if detail else "all mapped columns present",
          severity="critical" if (missing_src or missing_tgt) else "info",
          examples=missing_src + missing_tgt)
    if missing_src or missing_tgt:
        return _result(df_src, df_tgt, mapping, checks)

    # -- row counts --------------------------------------------------------
    na, nb = len(df_a), len(df_b)
    if mapping.cardinality == Cardinality.ONE_ONE:
        check("row_count", na == nb,
              f"source {na} rows vs target {nb} rows")
    else:
        check("row_count", True,
              f"source {na} rows, target {nb} rows ({mapping.cardinality.value})",
              severity="info")

    # -- keys ---------------------------------------------------------------
    has_keys = bool(src_key_cols)
    if has_keys:
        ka = _key_tuples(df_a, [k.get("tgt") or k["src"] for k in mapping.key_pairs])
        kb = _key_tuples(df_b, tgt_key_cols)

        dup_a = int(ka.duplicated().sum())
        dup_b = int(kb.duplicated().sum())
        need_uniq_a = mapping.cardinality in (Cardinality.ONE_ONE,
                                            Cardinality.ONE_MANY)
        need_uniq_b = mapping.cardinality in (Cardinality.ONE_ONE,
                                            Cardinality.MANY_ONE)
        if need_uniq_a:
            check("key_uniqueness_source", dup_a == 0,
                  f"{dup_a} duplicate key(s) in source "
                  f"(required unique for {mapping.cardinality.value})",
                  examples=_examples(ka[ka.duplicated(keep=False)].unique()))
        else:
            check("key_uniqueness_source", True,
                  f"{dup_a} repeated keys in source "
                  f"(expected for {mapping.cardinality.value})",
                  severity="info")
        if need_uniq_b:
            check("key_uniqueness_target", dup_b == 0,
                  f"{dup_b} duplicate key(s) in target "
                  f"(required unique for {mapping.cardinality.value})",
                  examples=_examples(kb[kb.duplicated(keep=False)].unique()))
        else:
            check("key_uniqueness_target", True,
                  f"{dup_b} repeated keys in target "
                  f"(expected for {mapping.cardinality.value})",
                  severity="info")

        set_a, set_b = set(ka.dropna()), set(kb.dropna())
        missing = set_a - set_b
        orphans = set_b - set_a
        # for N:1 aggregation, source keys that roll up below target grain are
        # expected — report as info rather than failure
        missing_sev = "error" if mapping.cardinality in (
            Cardinality.ONE_ONE, Cardinality.ONE_MANY) else "warning"
        check("missing_in_target", len(missing) == 0,
              f"{len(missing)} source key(s) have no target row",
              severity=missing_sev,
              examples=_examples(sorted(missing, key=str)))
        check("orphans_in_target", len(orphans) == 0,
              f"{len(orphans)} target key(s) have no source row",
              examples=_examples(sorted(orphans, key=str)))
    else:
        check("keys", True, "no key pairs defined — row-level checks skipped",
              severity="info")

    # -- value match --------------------------------------------------------
    val_cols = [cm for cm in mapping.columns
                if cm.tgt in df_b.columns
                and (cm.transform or (cm.src or "") in df_src.columns
                     or cm.tgt in df_a.columns)]
    if has_keys and val_cols:
        a = df_a.copy()
        a["__k__"] = ka
        b = df_b.copy()
        b["__k__"] = kb
        merged = a.merge(b, on="__k__", suffixes=("", "__t"),
                         indicator=False)
        total_mm = 0
        for cm in val_cols:
            s = merged[cm.tgt]
            tcol = cm.tgt + "__t"
            if tcol not in merged.columns:
                tcol = cm.tgt
            t = merged[tcol]
            both_null = s.isna() & t.isna()
            if cm.tolerance:
                sn = pd.to_numeric(s, errors="coerce")
                tn = pd.to_numeric(t, errors="coerce")
                diff = (sn - tn).abs() > cm.tolerance
                neq = diff & ~(both_null) & (sn.notna() | tn.notna())
            else:
                neq = (s.ne(t) | (s.isna() != t.isna())) & ~both_null
            nmm = int(neq.sum())
            total_mm += nmm
            if nmm:
                bad = merged[neq]
                ex = [{"key": str(k), "src": _examples([sv])[0],
                       "tgt": _examples([tv])[0]}
                      for k, sv, tv in zip(bad["__k__"].head(_MAX_EXAMPLES),
                                           bad[cm.tgt].head(_MAX_EXAMPLES),
                                           bad[tcol].head(_MAX_EXAMPLES))]
                check(f"value_match:{cm.tgt}", False,
                      f"{nmm} row(s) differ"
                      + (f" (tolerance {cm.tolerance})" if cm.tolerance else ""),
                      examples=ex)
        if total_mm == 0:
            check("value_match", True,
                  f"{len(merged)} joined row(s): all {len(val_cols)} mapped "
                  "column(s) match")
    elif val_cols:
        check("value_match", True,
              "value comparison needs key_pairs — skipped", severity="info")

    # -- aggregates ----------------------------------------------------------
    for agg in mapping.aggregates:
        # {column: X} compares X on both sides; {src: X, tgt: Y} compares
        # source column X (pre-rename) to target column Y
        op = agg.get("op", "sum")
        src_col = agg.get("src") or agg.get("column")
        tgt_col = agg.get("tgt") or agg.get("column")
        tol = float(agg.get("tolerance", 0) or 0)
        # the source column may be the original name or the renamed target name
        sa = src_col if src_col in df_a.columns else \
            next((cm.tgt for cm in mapping.columns
                  if cm.src == src_col), src_col)
        label = f"{op}({src_col}->{tgt_col})" if src_col != tgt_col \
            else f"{op}({tgt_col})"
        if sa not in df_a.columns or tgt_col not in df_b.columns:
            check(f"aggregate:{label}", False,
                  f"columns '{src_col}'/'{tgt_col}' not present on both sides")
            continue
        try:
            va = getattr(df_a[sa], op)()
            vb = getattr(df_b[tgt_col], op)()
            if pd.isna(va) and pd.isna(vb):
                diff_ok, diff = True, 0
            else:
                diff = abs(float(va) - float(vb))
                diff_ok = diff <= tol
            check(f"aggregate:{label}", diff_ok,
                  f"source {va} vs target {vb} (|diff|={diff:g}, tol={tol})")
        except Exception as e:
            check(f"aggregate:{label}", False, f"aggregate failed: {e}")

    return _result(df_src, df_tgt, mapping, checks)


def _result(df_src, df_tgt, mapping, checks) -> dict:
    failed = [c for c in checks if not c["passed"]]
    sev_rank = {"info": 0, "warning": 1, "error": 2, "critical": 3}
    worst = "info"
    for c in failed:
        if sev_rank[c["severity"]] > sev_rank[worst]:
            worst = c["severity"]
    # warnings are surfaced but do not fail the reconciliation
    real_failures = [c for c in failed
                     if sev_rank[c["severity"]] >= sev_rank["error"]]
    return {
        "mapping": mapping.name,
        "dataset_a": mapping.dataset_a,
        "dataset_b": mapping.dataset_b,
        "cardinality": mapping.cardinality.value,
        "row_count_a": int(len(df_src)),
        "row_count_b": int(len(df_tgt)),
        "passed": not real_failures,
        "worst_severity": worst if real_failures else "info",
        "checks": checks,
        "summary": {
            "checks_total": len(checks),
            "checks_passed": sum(1 for c in checks if c["passed"]),
            "checks_failed": len(real_failures),
        },
    }
