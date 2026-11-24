"""Rule execution engine + DQ scoring.

Runs a set of Rules against a DataFrame, producing a RunResult with:
  - per-rule pass/fail, pass-rate, failed examples
  - an overall weighted 0-100 DQ score
  - per-dimension sub-scores
Also persists runs to disk for history/trends.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import datetime as _dt
import pandas as pd

from ..core.config import Config
from ..core.models import (
    DQDimension, Rule, RuleResult, RunResult, Severity, to_jsonable,
)
from . import registry

# severity weighting for the score (critical failures hurt more)
_SEV_WEIGHT = {
    Severity.INFO: 0.25,
    Severity.WARNING: 0.5,
    Severity.ERROR: 1.0,
    Severity.CRITICAL: 2.0,
}


def evaluate_rule(df: pd.DataFrame, rule: Rule) -> RuleResult:
    spec = registry.get_rule(rule.rule_type)
    fn = spec["fn"]
    rows = int(len(df))
    try:
        failed_mask, msg = fn(df, rule.column, rule.params)
        failed_mask = failed_mask.fillna(False).astype(bool)
        rows_failed = int(failed_mask.sum())
        pass_rate = 1.0 if rows == 0 else (rows - rows_failed) / rows
        passed = pass_rate >= rule.threshold

        examples = []
        if rows_failed:
            idx = failed_mask[failed_mask].index[:5]
            if rule.column and rule.column in df.columns:
                examples = [to_jsonable(v) for v in df.loc[idx, rule.column].tolist()]
            else:
                examples = [{"row": int(i)} for i in idx]

        return RuleResult(
            rule_id=rule.id, rule_name=rule.name, rule_type=rule.rule_type,
            column=rule.column, dimension=rule.dimension, severity=rule.severity,
            passed=passed, rows_evaluated=rows, rows_failed=rows_failed,
            pass_rate=round(pass_rate, 4), threshold=rule.threshold,
            weight=rule.weight,
            message=f"{msg} — {rows_failed}/{rows} failed"
                    f" ({round(pass_rate*100,1)}% pass, need {round(rule.threshold*100,1)}%)",
            failed_examples=examples,
        )
    except Exception as e:
        return RuleResult(
            rule_id=rule.id, rule_name=rule.name, rule_type=rule.rule_type,
            column=rule.column, dimension=rule.dimension, severity=rule.severity,
            passed=False, rows_evaluated=rows, rows_failed=rows, pass_rate=0.0,
            threshold=rule.threshold, weight=rule.weight,
            message=f"rule error: {e}", error=str(e),
        )


def _score(results: list[RuleResult]) -> tuple[float, dict]:
    """Weighted DQ score 0-100 + per-dimension scores.

    Each rule contributes pass_rate, weighted by rule.weight * severity weight.
    """
    if not results:
        return 0.0, {}

    total_w = 0.0
    earned = 0.0
    dim_earned: dict[str, float] = {}
    dim_total: dict[str, float] = {}

    for r in results:
        if r.error:
            w = r.weight * _SEV_WEIGHT.get(r.severity, 1.0)
            contrib = 0.0
        else:
            w = r.weight * _SEV_WEIGHT.get(r.severity, 1.0)
            contrib = r.pass_rate * w
        total_w += w
        earned += contrib
        d = r.dimension.value
        dim_total[d] = dim_total.get(d, 0.0) + w
        dim_earned[d] = dim_earned.get(d, 0.0) + contrib

    score = round(100 * earned / total_w, 2) if total_w else 0.0
    dim_scores = {
        d: round(100 * dim_earned[d] / dim_total[d], 2)
        for d in dim_total if dim_total[d]
    }
    return score, dim_scores


def run_rules(df: pd.DataFrame, rules: list[Rule], dataset: str,
              cfg: Optional[Config] = None, persist: bool = True) -> RunResult:
    cfg = cfg or Config.load()
    active = [r for r in rules if r.enabled]
    results = [evaluate_rule(df, r) for r in active]
    score, dim_scores = _score(results)

    run = RunResult(
        dataset=dataset,
        results=results,
        score=score,
        dimension_scores=dim_scores,
        total_rules=len(results),
        passed_rules=sum(1 for r in results if r.passed),
        failed_rules=sum(1 for r in results if not r.passed),
        row_count=int(len(df)),
        finished_at=_dt.datetime.now(_dt.timezone.utc).isoformat(),
    )
    if persist:
        save_run(run, cfg)
    return run


def save_run(run: RunResult, cfg: Config) -> Path:
    runs_dir = Path(cfg.app_dir) / "runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    path = runs_dir / f"{run.started_at.replace(':', '-')}_{run.id}.json"
    path.write_text(json.dumps(to_jsonable(run), indent=2, default=str))
    return path


def load_run_history(dataset: Optional[str], cfg: Config, limit: int = 50) -> list[dict]:
    runs_dir = Path(cfg.app_dir) / "runs"
    if not runs_dir.exists():
        return []
    out = []
    for p in sorted(runs_dir.glob("*.json"), reverse=True):
        try:
            data = json.loads(p.read_text())
        except Exception:
            continue
        if dataset and data.get("dataset") != dataset:
            continue
        out.append({
            "id": data.get("id"), "dataset": data.get("dataset"),
            "score": data.get("score"), "started_at": data.get("started_at"),
            "passed_rules": data.get("passed_rules"),
            "failed_rules": data.get("failed_rules"),
            "dimension_scores": data.get("dimension_scores", {}),
        })
        if len(out) >= limit:
            break
    return out
