"""Batch execution — run a manifest of validation jobs in one pass.

A manifest (YAML or JSON) describes any mix of:

  audit      profile + rules + score (+ optional schema gate) for a dataset
  reconcile  source->target reconciliation via a saved or inline mapping
  gate       schema-contract gate for a dataset (baseline must be pinned)
  pair       convenience: one file validated against one table
             (auto-registers the file as a source; `columns: auto` maps
             every same-named column)

Example manifest:

    name: nightly-etl-check
    parallelism: 4
    jobs:
      - name: orders-audit
        type: audit
        source: landing
        dataset: orders.csv
        gate: true
        fail_under: 80
      - name: orders-recon
        type: reconcile
        mapping: orders_to_stg          # saved mapping name
      - name: stg-customers-gate
        type: gate
        source: staging
        dataset: stg_customers
      - name: products-pair
        type: pair
        source_file: samples/products.csv
        target_source: staging
        target_dataset: stg_products
        mapping:
          cardinality: "1:1"
          key_pairs: [{src: sku, tgt: sku}]
          columns: auto

Exit-code contract: `run_batch` returns a report dict; callers use
`report["passed"]` — CLI exits non-zero when False, which is what an ECS
task / CI pipeline needs to break the batch.
"""
from __future__ import annotations

import datetime as _dt
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Optional

from .models import DatasetMapping


def load_manifest(path: str) -> dict:
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Manifest not found: {path}")
    text = p.read_text()
    if p.suffix.lower() in (".yaml", ".yml"):
        import yaml  # type: ignore
        return yaml.safe_load(text) or {}
    import json
    return json.loads(text)


def _resolve_mapping(svc, job: dict) -> DatasetMapping:
    m = job.get("mapping")
    if isinstance(m, str):
        saved = svc.get_mapping(m)
        if saved is None:
            raise KeyError(f"Mapping '{m}' not found")
        return saved
    if job.get("type") == "pair":
        spec = dict(m or {})
        spec.setdefault("name", job.get("name", "pair"))
        spec["source_a"] = job.get("_src_a") or job.get("source") \
            or spec.get("source_a")
        spec["dataset_a"] = job.get("_ds_a") or job.get("dataset") \
            or spec.get("dataset_a")
        spec["source_b"] = job.get("target_source") or spec.get("source_b")
        spec["dataset_b"] = job.get("target_dataset") or spec.get("dataset_b")
        return DatasetMapping(**{k: v for k, v in spec.items()
                                 if k in DatasetMapping.__dataclass_fields__})
    if isinstance(m, dict):
        return DatasetMapping(**{k: v for k, v in m.items()
                                 if k in DatasetMapping.__dataclass_fields__})
    raise ValueError(f"job '{job.get('name')}' needs a mapping (name or spec)")


def _run_job(svc, job: dict) -> dict:
    jtype = job.get("type", "audit")
    name = job.get("name") or job.get("dataset") or jtype
    out = {"name": name, "type": jtype, "passed": False}

    if jtype == "audit":
        data = svc.audit(job["source"], job["dataset"], use_ai=job.get("use_ai", False))
        score = (data.get("run") or {}).get("score", 0.0)
        out.update(score=score,
                   passed=score >= float(job.get("fail_under", 0)))
        if job.get("gate"):
            g = svc.schema_gate(job["source"], job["dataset"],
                                pin_if_missing=job.get("pin_if_missing", False))
            out["gate"] = g
            out["passed"] = out["passed"] and g["passed"]

    elif jtype == "reconcile":
        mp = _resolve_mapping(svc, job)
        res = svc.reconcile(mp)
        out.update(passed=res["passed"], summary=res["summary"],
                   checks=res["checks"], worst=res["worst_severity"])

    elif jtype == "gate":
        g = svc.schema_gate(job["source"], job["dataset"],
                            version=job.get("version"),
                            pin_if_missing=job.get("pin_if_missing", False))
        out.update(passed=g["passed"], gate=g)

    elif jtype == "pair":
        mp = _resolve_mapping(svc, job)
        res = svc.reconcile(mp)
        out.update(passed=res["passed"], summary=res["summary"],
                   checks=res["checks"], worst=res["worst_severity"])
        if job.get("gate"):
            g = svc.schema_gate(job.get("_src_a") or job.get("source"),
                                job.get("_ds_a") or job.get("dataset"),
                                pin_if_missing=job.get("pin_if_missing", False))
            out["gate"] = g
            out["passed"] = out["passed"] and g["passed"]

    else:
        raise ValueError(f"unknown job type '{jtype}'")
    return out


def run_batch(svc, manifest: dict,
              parallelism: Optional[int] = None,
              fail_fast: bool = False) -> dict:
    jobs = list(manifest.get("jobs", []))
    workers = int(parallelism or manifest.get("parallelism")
                  or svc.cfg.parallel_workers or 1)
    fail_fast = bool(fail_fast or manifest.get("fail_fast"))

    # pre-register file sources declared in the manifest (deterministic
    # temp names). source_file works on any job; pair jobs may also give a
    # target_file for file<->file reconciliation.
    for i, job in enumerate(jobs):
        if job.get("source_file"):
            fname = job["source_file"]
            src_name = f"batch_{i}_{Path(fname).stem}"
            svc.register_source(src_name, "file", path=fname,
                                **(job.get("options") or {}))
            datasets = svc.list_datasets(src_name)
            job["_src_a"] = src_name
            job["_ds_a"] = job.get("dataset") or datasets[0]
            job.setdefault("source", src_name)
            job.setdefault("dataset", job["_ds_a"])
        if job.get("target_file"):
            fname = job["target_file"]
            src_name = f"batch_{i}_{Path(fname).stem}_tgt"
            svc.register_source(src_name, "file", path=fname,
                                **(job.get("target_options") or {}))
            datasets = svc.list_datasets(src_name)
            job["target_source"] = src_name
            job.setdefault("target_dataset", datasets[0])

    results: list[dict] = []
    started = _dt.datetime.now(_dt.timezone.utc)
    with ThreadPoolExecutor(max_workers=max(workers, 1)) as pool:
        futs = {pool.submit(_run_job, svc, j): j for j in jobs}
        for fut in as_completed(futs):
            job = futs[fut]
            try:
                results.append(fut.result())
            except Exception as e:
                results.append({"name": job.get("name", "?"),
                                "type": job.get("type", "?"),
                                "passed": False, "error": str(e)})
            if fail_fast and not results[-1]["passed"]:
                for f in futs:
                    f.cancel()
                break
    # restore manifest order for readability
    order = {id(j): i for i, j in enumerate(jobs)}
    results.sort(key=lambda r: min(
        (order[id(j)] for j in jobs
         if j.get("name", j.get("dataset", "")) == r["name"]),
        default=len(jobs)))

    passed = all(r["passed"] for r in results)
    return {
        "name": manifest.get("name", "batch"),
        "started_at": started.isoformat(),
        "finished_at": _dt.datetime.now(_dt.timezone.utc).isoformat(),
        "passed": passed,
        "jobs_total": len(results),
        "jobs_failed": sum(1 for r in results if not r["passed"]),
        "results": results,
    }
