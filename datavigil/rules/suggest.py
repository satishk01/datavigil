"""Auto-suggest data quality rules from a TableProfile.

Gives users a strong starting rule set with zero authoring, which they can
then edit, disable, or extend (conversationally via the LLM layer).
"""
from __future__ import annotations

from ..core.models import DQDimension, Rule, Severity, TableProfile
from ..profiling.patterns import NAMED_PATTERNS

_NAMED_REGEX = {name: rx.pattern for name, rx in NAMED_PATTERNS}


def suggest_rules(profile: TableProfile) -> list[Rule]:
    rules: list[Rule] = []
    for col in profile.columns:
        c = col.name

        # Completeness: if a column is mostly populated, require it stays so
        if col.null_pct <= 1.0 and col.count > 0:
            rules.append(Rule(
                name=f"{c} not null", rule_type="not_null", column=c,
                dimension=DQDimension.COMPLETENESS, severity=Severity.ERROR,
                threshold=1.0, description="Column is fully populated in current data",
                source="suggested",
            ))
        elif col.null_pct <= 10.0:
            rules.append(Rule(
                name=f"{c} mostly populated", rule_type="completeness_pct", column=c,
                dimension=DQDimension.COMPLETENESS, severity=Severity.WARNING,
                threshold=0.9, description="Column stays at least 90% populated",
                source="suggested",
            ))

        # Uniqueness: looks like a key
        if col.is_unique and col.count > 0:
            rules.append(Rule(
                name=f"{c} unique", rule_type="unique", column=c,
                dimension=DQDimension.UNIQUENESS, severity=Severity.ERROR,
                threshold=1.0, description="Column is a candidate key",
                source="suggested",
            ))

        # Validity by inferred type
        if col.inferred_type == "numeric" and col.min is not None:
            # widen observed range by 20% so normal variance passes
            span = (col.max - col.min) or abs(col.max) or 1
            lo = col.min - 0.2 * span
            hi = col.max + 0.2 * span
            rules.append(Rule(
                name=f"{c} within expected range", rule_type="range", column=c,
                params={"min": round(lo, 4), "max": round(hi, 4)},
                dimension=DQDimension.VALIDITY, severity=Severity.WARNING,
                threshold=0.99, description="Value stays near the observed range",
                source="suggested",
            ))

        elif col.inferred_type == "categorical" and col.top_values:
            allowed = [v["value"] for v in col.top_values if v["value"] is not None]
            if allowed:
                rules.append(Rule(
                    name=f"{c} in known set", rule_type="in_set", column=c,
                    params={"values": allowed},
                    dimension=DQDimension.VALIDITY, severity=Severity.WARNING,
                    threshold=0.99, description="Value is from the observed category set",
                    source="suggested",
                ))

        elif col.inferred_type == "datetime":
            rules.append(Rule(
                name=f"{c} parses as date", rule_type="dtype", column=c,
                params={"type": "datetime"},
                dimension=DQDimension.VALIDITY, severity=Severity.ERROR,
                threshold=0.99, description="Values are valid dates",
                source="suggested",
            ))

        elif col.inferred_type == "string" and col.patterns:
            top = col.patterns[0]
            if top.get("kind") == "named" and top["pct"] >= 90 and top["pattern"] in _NAMED_REGEX:
                rules.append(Rule(
                    name=f"{c} matches {top['pattern']}", rule_type="regex", column=c,
                    params={"pattern": _NAMED_REGEX[top["pattern"]]},
                    dimension=DQDimension.VALIDITY, severity=Severity.WARNING,
                    threshold=0.98,
                    description=f"Values follow the {top['pattern']} format",
                    source="suggested",
                ))

    return rules
