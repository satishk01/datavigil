"""Pattern inference for string columns.

Two layers:
  1. Named-format detection (email, phone, UUID, date, URL, IP, ZIP, etc.)
  2. Structural "masks" — collapse each value to a shape (A=letter, 9=digit,
     x=other) so recurring formats surface even when unnamed.
"""
from __future__ import annotations

import re
from collections import Counter
from typing import Optional

NAMED_PATTERNS: list[tuple[str, re.Pattern]] = [
    ("email", re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")),
    ("url", re.compile(r"^https?://", re.I)),
    ("uuid", re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$", re.I)),
    ("ipv4", re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")),
    ("us_phone", re.compile(r"^\+?1?[\s.-]?\(?\d{3}\)?[\s.-]?\d{3}[\s.-]?\d{4}$")),
    ("us_zip", re.compile(r"^\d{5}(-\d{4})?$")),
    ("iso_date", re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?")),
    ("us_date", re.compile(r"^\d{1,2}/\d{1,2}/\d{2,4}$")),
    ("integer_str", re.compile(r"^-?\d+$")),
    ("decimal_str", re.compile(r"^-?\d+\.\d+$")),
    ("currency", re.compile(r"^[$€£¥]\s?-?\d[\d,]*(\.\d+)?$")),
    # HTS / tariff codes (relevant to trade-compliance datasets): 6-10 digits,
    # often dotted 4.4.2
    ("hts_code", re.compile(r"^\d{4}\.?\d{2}\.?\d{2,4}$")),
]


def structural_mask(value: str, max_len: int = 40) -> str:
    out = []
    for ch in value[:max_len]:
        if ch.isalpha():
            out.append("A")
        elif ch.isdigit():
            out.append("9")
        elif ch.isspace():
            out.append(" ")
        else:
            out.append(ch)   # keep separators literal (-, /, ., @ etc.)
    mask = "".join(out)
    # collapse runs: AAAA -> A{4}
    return _collapse_runs(mask)


def _collapse_runs(mask: str) -> str:
    out = []
    i = 0
    n = len(mask)
    while i < n:
        ch = mask[i]
        j = i
        while j < n and mask[j] == ch:
            j += 1
        run = j - i
        if ch in ("A", "9") and run > 1:
            out.append(f"{ch}{{{run}}}")
        else:
            out.append(ch * run)
        i = j
    return "".join(out)


def infer_patterns(values: list[str], top_n: int = 5) -> list[dict]:
    """Return the most common patterns among string values."""
    values = [v for v in values if isinstance(v, str) and v != ""]
    if not values:
        return []
    total = len(values)

    # 1) named formats — if one dominates, report it
    named = Counter()
    for v in values:
        for name, rx in NAMED_PATTERNS:
            if rx.match(v):
                named[name] += 1
                break
    results: list[dict] = []
    for name, cnt in named.most_common(top_n):
        example = next((v for v in values if _matches_named(v, name)), None)
        results.append({
            "pattern": name,
            "count": cnt,
            "pct": round(100 * cnt / total, 2),
            "example": example,
            "kind": "named",
        })

    # 2) structural masks for the remainder
    masks = Counter(structural_mask(v) for v in values)
    for mask, cnt in masks.most_common(top_n):
        example = next((v for v in values if structural_mask(v) == mask), None)
        results.append({
            "pattern": mask,
            "count": cnt,
            "pct": round(100 * cnt / total, 2),
            "example": example,
            "kind": "mask",
        })
    return results


def _matches_named(value: str, name: str) -> bool:
    for n, rx in NAMED_PATTERNS:
        if n == name:
            return bool(rx.match(value))
    return False


def dominant_named_pattern(values: list[str], threshold: float = 0.9) -> Optional[str]:
    """Return a named pattern if >= threshold of non-null values match it."""
    vals = [v for v in values if isinstance(v, str) and v != ""]
    if not vals:
        return None
    for name, rx in NAMED_PATTERNS:
        matches = sum(1 for v in vals if rx.match(v))
        if matches / len(vals) >= threshold:
            return name
    return None
