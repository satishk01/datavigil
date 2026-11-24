"""Schema contracts — pin a baseline schema, detect breaking changes later.

A "schema gate" is the pre-ingestion check a batch pipeline calls before
loading new data: the incoming dataset's schema is compared against the
pinned baseline and every difference is classified:

  breaking     column removed, or inferred type changed (e.g. string -> int)
               — these are the changes that would fail the load downstream
  warning      raw dtype drift inside the same inferred family
               (int64 -> float64)
  compatible   column added — non-breaking for almost every pipeline

Baselines are versioned on disk under <app_dir>/schemas/ so 'v1' from last
month is still comparable today.
"""
from __future__ import annotations

import datetime as _dt
import json
import re
from pathlib import Path
from typing import Optional

from .models import TableProfile

SCHEMA_DIR_NAME = "schemas"


def _safe(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name)


def snapshot_columns(profile: TableProfile) -> list[dict]:
    return [{"name": c.name, "type": c.inferred_type, "dtype": c.dtype}
            for c in profile.columns]


def _contract_path(app_dir: str, dataset_key: str) -> Path:
    return Path(app_dir) / SCHEMA_DIR_NAME / f"{_safe(dataset_key)}.json"


def _load_contract(path: Path) -> dict:
    if not path.exists():
        return {"versions": []}
    try:
        return json.loads(path.read_text())
    except Exception:
        return {"versions": []}


def pin_baseline(app_dir: str, dataset_key: str, profile: TableProfile) -> dict:
    """Store a new versioned schema snapshot. Returns the version entry."""
    path = _contract_path(app_dir, dataset_key)
    path.parent.mkdir(parents=True, exist_ok=True)
    contract = _load_contract(path)
    version = len(contract["versions"]) + 1
    entry = {
        "version": version,
        "captured_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "row_count": profile.row_count,
        "columns": snapshot_columns(profile),
    }
    contract["dataset"] = dataset_key
    contract["versions"].append(entry)
    path.write_text(json.dumps(contract, indent=2, default=str))
    return entry


def list_versions(app_dir: str, dataset_key: str) -> list[dict]:
    c = _load_contract(_contract_path(app_dir, dataset_key))
    return [{"version": v["version"], "captured_at": v["captured_at"],
             "row_count": v.get("row_count"),
             "column_count": len(v.get("columns", []))}
            for v in c.get("versions", [])]


def _baseline(app_dir: str, dataset_key: str,
              version: Optional[int] = None) -> Optional[dict]:
    versions = _load_contract(
        _contract_path(app_dir, dataset_key)).get("versions", [])
    if not versions:
        return None
    if version is None:
        return versions[-1]
    return next((v for v in versions if v["version"] == version), None)


def diff_schemas(old_cols: list[dict], new_cols: list[dict]) -> dict:
    """Classified schema diff. breaking=True fails the ingestion gate."""
    old = {c["name"]: c for c in old_cols}
    new = {c["name"]: c for c in new_cols}
    changes: list[dict] = []

    for name in sorted(set(old) - set(new)):
        changes.append({"column": name, "kind": "column_removed",
                        "from": old[name]["type"], "to": None,
                        "classification": "breaking"})
    for name in sorted(set(new) - set(old)):
        changes.append({"column": name, "kind": "column_added",
                        "from": None, "to": new[name]["type"],
                        "classification": "compatible"})
    for name in sorted(set(old) & set(new)):
        o, n = old[name], new[name]
        if o["type"] != n["type"]:
            changes.append({"column": name, "kind": "type_changed",
                            "from": o["type"], "to": n["type"],
                            "classification": "breaking"})
        elif o["dtype"] != n["dtype"]:
            changes.append({"column": name, "kind": "dtype_drift",
                            "from": o["dtype"], "to": n["dtype"],
                            "classification": "warning"})
    breaking = [c for c in changes if c["classification"] == "breaking"]
    warnings = [c for c in changes if c["classification"] == "warning"]
    return {
        "breaking": breaking,
        "warnings": warnings,
        "compatible": [c for c in changes
                       if c["classification"] == "compatible"],
        "changed": bool(changes),
        "passed": not breaking,
    }


def gate(app_dir: str, dataset_key: str, profile: TableProfile,
         version: Optional[int] = None,
         pin_if_missing: bool = False) -> dict:
    """Compare `profile` against the pinned baseline for `dataset_key`."""
    base = _baseline(app_dir, dataset_key, version)
    if base is None:
        if pin_if_missing:
            entry = pin_baseline(app_dir, dataset_key, profile)
            return {"dataset": dataset_key, "baseline_version": entry["version"],
                    "passed": True, "changed": False, "breaking": [],
                    "warnings": [], "compatible": [],
                    "message": "no baseline existed — pinned current schema "
                               f"as v{entry['version']}"}
        return {"dataset": dataset_key, "baseline_version": None,
                "passed": False, "changed": True,
                "breaking": [{"column": None, "kind": "no_baseline",
                              "classification": "breaking",
                              "from": None, "to": None}],
                "warnings": [], "compatible": [],
                "message": "no baseline pinned for this dataset — "
                           "run `schema pin` first"}
    diff = diff_schemas(base["columns"], snapshot_columns(profile))
    diff.update({
        "dataset": dataset_key,
        "baseline_version": base["version"],
        "baseline_captured_at": base["captured_at"],
        "message": ("PASS — schema compatible with baseline "
                    f"v{base['version']}" if diff["passed"] else
                    f"FAIL — {len(diff['breaking'])} breaking change(s) "
                    f"vs baseline v{base['version']}"),
    })
    return diff
