"""High-level orchestration — the facade the API, CLI, and tests all use.

Holds registered data sources and saved rule sets, and exposes the end-to-end
operations: profile, suggest rules, run rules, detect anomalies, NL authoring,
and LLM insights.
"""
from __future__ import annotations

import json
import re
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Optional, Union

import pandas as pd

from .config import Config
from .models import (
    ColumnMap, DatasetMapping, Rule, RunResult, TableProfile, to_jsonable,
)
from . import schema_contract
from ..connectors import get_connector, Connector
from ..profiling import profile_dataframe, compare_tables
from ..reconcile import reconcile as _reconcile
from ..rules import run_rules, suggest_rules, load_run_history
from ..anomaly import detect
from ..llm import (
    LLMProvider, NullProvider, nl_to_rules, profile_insights, ask, analyze_run,
)


class DataVigilService:
    def __init__(self, cfg: Optional[Config] = None):
        self.cfg = cfg or Config.load()
        self.llm = LLMProvider.from_config(self.cfg.llm)
        self._sources: dict[str, dict] = {}
        self._rulesets: dict[str, list[Rule]] = {}
        self._mappings: dict[str, DatasetMapping] = {}
        self._profiles: dict[str, TableProfile] = {}
        self._last_runs: dict[str, RunResult] = {}
        self._last_audits: dict[str, dict] = {}
        self._last_schema_audits: dict[str, dict] = {}
        self._local = threading.local()   # per-thread connector cache
        self._load_state()

    # -- source registration ----------------------------------------------
    def register_source(self, name: str, source_type: str, **params) -> dict:
        # validate it connects
        conn = get_connector(source_type, **params)
        datasets = conn.list_datasets()
        conn.close()
        self._sources[name] = {"type": source_type, "params": params}
        self._save_state()
        return {"name": name, "type": source_type, "datasets": datasets}

    def list_sources(self) -> list[dict]:
        return [{"name": n, "type": s["type"]} for n, s in self._sources.items()]

    def delete_source(self, name: str) -> Optional[dict]:
        """Unregister a source and drop its cached state. Returns the removed
        source info (type/params) or None if unknown."""
        info = self._sources.pop(name, None)
        if info is None:
            return None
        conns = getattr(self._local, "conns", None)
        conn = conns.pop(name, None) if conns else None
        if conn:
            try:
                conn.close()
            except Exception:
                pass
        prefix = f"{name}."
        for cache in (self._profiles, self._rulesets, self._last_runs,
                      self._last_audits):
            for k in [k for k in cache if k.startswith(prefix)]:
                cache.pop(k, None)
        self._last_schema_audits.pop(name, None)
        self._save_state()
        return info

    def _connector(self, source: str) -> Connector:
        """Shared connector per source per thread — keeps pools and the MCP
        server process alive across operations, while staying safe for the
        parallel batch / schema-audit paths (each worker thread gets its own
        connector instance; SQLAlchemy engines pool internally anyway)."""
        if source not in self._sources:
            raise KeyError(f"Unknown source: {source}")
        conns = getattr(self._local, "conns", None)
        if conns is None:
            conns = self._local.conns = {}
        conn = conns.get(source)
        if conn is None:
            s = self._sources[source]
            conn = get_connector(s["type"], **s["params"])
            conns[source] = conn
        return conn

    def list_datasets(self, source: str) -> list[str]:
        return self._connector(source).list_datasets()

    def load(self, source: str, dataset: str, limit: Optional[int] = None) -> pd.DataFrame:
        # default cap protects the tester UI / reports from multi-million-row loads
        effective = limit if limit is not None else self.cfg.max_profile_sample
        return self._connector(source).load(dataset, limit=effective)

    def _load_and_count(self, source: str, dataset: str):
        """One connector round-trip: sampled rows + true row count."""
        conn = self._connector(source)
        df = conn.load(dataset, limit=self.cfg.max_profile_sample)
        return df, conn.row_count(dataset)

    # -- profiling ----------------------------------------------------------
    def profile(self, source: str, dataset: str, with_insights: bool = False,
                df: Optional[pd.DataFrame] = None,
                row_count: Optional[int] = None) -> TableProfile:
        if df is None:
            df, real_count = self._load_and_count(source, dataset)
        else:
            real_count = row_count
        prof = profile_dataframe(df, dataset=f"{source}.{dataset}", cfg=self.cfg)
        if real_count is not None and real_count > prof.row_count:
            prof.profiled_rows = prof.row_count   # rows actually analysed
            prof.row_count = real_count           # true table/file size
        if with_insights and self.llm.available:
            try:
                prof.llm_summary = profile_insights(prof, self.llm)
            except Exception as e:
                prof.llm_summary = f"(insight generation failed: {e})"
        self._profiles[f"{source}.{dataset}"] = prof
        return prof

    def compare(self, source_a, dataset_a, source_b, dataset_b) -> dict:
        pa = self.profile(source_a, dataset_a)
        pb = self.profile(source_b, dataset_b)
        return compare_tables(pa, pb)

    # -- mappings (source -> target reconciliation) -------------------------
    def list_mappings(self) -> list[dict]:
        return [to_jsonable(m) for m in self._mappings.values()]

    def get_mapping(self, name: str) -> Optional[DatasetMapping]:
        return self._mappings.get(name)

    def set_mapping(self, mapping: DatasetMapping) -> DatasetMapping:
        self._mappings[mapping.name] = mapping
        self._save_state()
        return mapping

    def delete_mapping(self, name: str) -> bool:
        if name in self._mappings:
            del self._mappings[name]
            self._save_state()
            return True
        return False

    def suggest_mapping(self, source_a: str, dataset_a: str,
                        source_b: str, dataset_b: str) -> dict:
        """Auto-propose a mapping: same-named columns pair up, columns unique
        on both sides are offered as candidate keys."""
        pa = self.profile(source_a, dataset_a)
        pb = self.profile(source_b, dataset_b)
        cols_a = {c.name: c for c in pa.columns}
        cols_b = {c.name: c for c in pb.columns}
        shared = sorted(set(cols_a) & set(cols_b))
        keyish = re.compile(r"(^|_)(id|key|no|num|number|code|pk|sk)($|_)|"
                            r"(id|key|num|no|code)$", re.IGNORECASE)
        key_candidates = sorted(
            [{"src": c, "tgt": c} for c in shared
             if cols_a[c].is_unique and cols_b[c].is_unique],
            key=lambda k: (not keyish.search(k["src"]), k["src"]),
        )
        columns = [{"src": c, "tgt": c, "transform": None, "tolerance": 0.0}
                   for c in shared]
        return {
            "name": f"{dataset_a}_to_{dataset_b}",
            "source_a": source_a, "dataset_a": dataset_a,
            "source_b": source_b, "dataset_b": dataset_b,
            "cardinality": "1:1",
            "key_pairs": key_candidates[:2],
            "columns": columns,
            "aggregates": [],
            "unmatched_source": sorted(set(cols_a) - set(cols_b)),
            "unmatched_target": sorted(set(cols_b) - set(cols_a)),
        }

    def reconcile(self, mapping: Union[str, DatasetMapping, dict]) -> dict:
        if isinstance(mapping, str):
            mp = self._mappings.get(mapping)
            if mp is None:
                raise KeyError(f"Mapping '{mapping}' not found")
        elif isinstance(mapping, dict):
            mapping = {k: v for k, v in mapping.items()
                       if k in DatasetMapping.__dataclass_fields__}
            mp = DatasetMapping(**mapping)
        else:
            mp = mapping
        df_a = self.load(mp.source_a, mp.dataset_a)
        df_b = self.load(mp.source_b, mp.dataset_b)
        if mp.columns == "auto":
            keys = {k.get("tgt") or k.get("src") for k in mp.key_pairs}
            mp.columns = [ColumnMap(src=c, tgt=c) for c in df_a.columns
                          if c in df_b.columns and c not in keys]
        return _reconcile(df_a, df_b, mp)

    # -- schema contracts (baseline -> gate) ---------------------------------
    def pin_schema(self, source: str, dataset: str) -> dict:
        prof = self.profile(source, dataset)
        return schema_contract.pin_baseline(
            self.cfg.app_dir, f"{source}.{dataset}", prof)

    def schema_versions(self, source: str, dataset: str) -> list[dict]:
        return schema_contract.list_versions(
            self.cfg.app_dir, f"{source}.{dataset}")

    def schema_gate(self, source: str, dataset: str,
                    version: Optional[int] = None,
                    pin_if_missing: bool = False,
                    df: Optional[pd.DataFrame] = None) -> dict:
        prof = self.profile(source, dataset, df=df)
        return schema_contract.gate(
            self.cfg.app_dir, f"{source}.{dataset}", prof,
            version=version, pin_if_missing=pin_if_missing)

    def schema_diff(self, source: str, dataset: str,
                    version: Optional[int] = None) -> dict:
        return self.schema_gate(source, dataset, version=version)

    # -- rules --------------------------------------------------------------
    def suggest(self, source: str, dataset: str) -> list[Rule]:
        prof = self.profile(source, dataset)
        rules = suggest_rules(prof)
        self._rulesets[f"{source}.{dataset}"] = rules
        self._save_state()
        return rules

    def set_rules(self, source: str, dataset: str, rules: list[Rule]) -> None:
        self._rulesets[f"{source}.{dataset}"] = rules
        self._save_state()

    def add_rules(self, source: str, dataset: str, rules: list[Rule]) -> list[Rule]:
        key = f"{source}.{dataset}"
        self._rulesets.setdefault(key, [])
        self._rulesets[key].extend(rules)
        self._save_state()
        return self._rulesets[key]

    def get_rules(self, source: str, dataset: str) -> list[Rule]:
        return self._rulesets.get(f"{source}.{dataset}", [])

    def nl_add_rules(self, source: str, dataset: str, request: str) -> list[Rule]:
        prof = self._profiles.get(f"{source}.{dataset}") or self.profile(source, dataset)
        new_rules = nl_to_rules(request, self.llm, profile=prof)
        return self.add_rules(source, dataset, new_rules)

    def run(self, source: str, dataset: str,
            rules: Optional[list[Rule]] = None,
            df: Optional[pd.DataFrame] = None) -> RunResult:
        if df is None:
            df = self.load(source, dataset)
        rules = rules if rules is not None else self.get_rules(source, dataset)
        if not rules:   # nothing defined yet — suggest on the fly
            prof = profile_dataframe(df, dataset=f"{source}.{dataset}", cfg=self.cfg)
            rules = suggest_rules(prof)
            self.set_rules(source, dataset, rules)
        result = run_rules(df, rules, dataset=f"{source}.{dataset}", cfg=self.cfg)
        self._last_runs[f"{source}.{dataset}"] = result
        return result

    def history(self, source: Optional[str] = None,
                dataset: Optional[str] = None) -> list[dict]:
        key = f"{source}.{dataset}" if source and dataset else None
        return load_run_history(key, self.cfg)

    # -- anomalies ----------------------------------------------------------
    def anomalies(self, source: str, dataset: str,
                  methods: Optional[list[str]] = None,
                  df: Optional[pd.DataFrame] = None):
        if df is None:
            df = self.load(source, dataset)
        return detect(df, dataset=f"{source}.{dataset}", methods=methods, cfg=self.cfg)

    # -- conversational -----------------------------------------------------
    def ask(self, source: str, dataset: str, question: str) -> str:
        key = f"{source}.{dataset}"
        prof = self._profiles.get(key)
        context = {"profile": to_jsonable(prof) if prof else None}
        hist = self.history(source, dataset)
        if hist:
            context["latest_run"] = hist[0]
        return ask(question, context, self.llm)

    def llm_health(self) -> dict:
        return self.llm.health()

    def update_llm(self, **kwargs) -> dict:
        """Reconfigure the LLM at runtime (from the settings UI) and persist."""
        for k, v in kwargs.items():
            if hasattr(self.cfg.llm, k) and v is not None:
                setattr(self.cfg.llm, k, v)
        self.cfg.save()
        self.llm = LLMProvider.from_config(self.cfg.llm)
        return self.llm.health()

    # -- AI analysis --------------------------------------------------------
    def analyze(self, source: str, dataset: str) -> dict:
        """Root-cause analysis of the latest run for this dataset."""
        key = f"{source}.{dataset}"
        run = self._last_runs.get(key) or self.run(source, dataset)
        prof = self._profiles.get(key)
        return analyze_run(run, self.llm, profile=prof)

    # -- one-click audit + report -------------------------------------------
    def audit(self, source: str, dataset: str, use_ai: bool = True) -> dict:
        """Full pipeline for non-technical users:
        profile → suggest rules (if none) → run & score → anomalies → AI analysis.
        Returns the report-ready bundle (also cached for report export).
        """
        key = f"{source}.{dataset}"
        df, real_count = self._load_and_count(source, dataset)
        prof = self.profile(source, dataset, with_insights=use_ai,
                            df=df, row_count=real_count)
        if not self.get_rules(source, dataset):
            self._rulesets[key] = suggest_rules(prof)
            self._save_state()
        run = self.run(source, dataset, df=df)
        try:
            anomalies = self.anomalies(source, dataset, df=df)
        except Exception:
            anomalies = None
        analysis = analyze_run(run, self.llm if use_ai else NullProvider(self.cfg.llm),
                               profile=prof)

        from ..report import build_report_data
        data = build_report_data(
            dataset=key, profile=prof, run=run, anomalies=anomalies,
            analysis=analysis, history=self.history(source, dataset))
        self._last_audits[key] = data
        return data

    def report(self, source: str, dataset: str, fmt: str = "docx",
               out_dir: Optional[str] = None, use_ai: bool = True) -> str:
        """Run (or reuse) an audit and render it to a DOCX/PDF file. Returns path."""
        from ..report import render_report
        key = f"{source}.{dataset}"
        data = self._last_audits.get(key) or self.audit(source, dataset, use_ai=use_ai)
        out = Path(out_dir) if out_dir else Path(self.cfg.app_dir) / "reports"
        out.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in key)
        stamp = data["generated_at"][:19].replace(":", "-").replace("T", "_")
        path = str(out / f"datavigil_report_{safe}_{stamp}.{fmt.lower()}")
        return render_report(data, path, fmt)

    # -- whole-schema audit --------------------------------------------------
    def audit_schema(self, source: str, use_ai: bool = True,
                     max_tables: Optional[int] = None,
                     workers: Optional[int] = None) -> dict:
        """Audit every table/dataset in a source — the one-click
        'audit the whole schema' flow for non-technical testers.

        Tables are audited in parallel (cfg.parallel_workers). Each table gets
        the standard audit pipeline; one bad table is reported in `errors`
        instead of aborting the run.
        """
        max_tables = max_tables or self.cfg.audit_max_tables
        workers = max(workers or self.cfg.parallel_workers, 1)
        datasets = self.list_datasets(source)
        audits: dict[str, dict] = {}
        errors: dict[str, str] = {}
        todo = datasets[:max_tables]

        def _one(ds):
            return ds, self.audit(source, ds, use_ai=use_ai)

        if workers == 1 or len(todo) <= 1:
            for ds in todo:
                try:
                    audits[ds] = self.audit(source, ds, use_ai=use_ai)
                except Exception as e:  # noqa: BLE001 — per-table resilience
                    errors[ds] = str(e)[:300]
        else:
            with ThreadPoolExecutor(max_workers=workers) as pool:
                futs = {pool.submit(_one, ds): ds for ds in todo}
                for fut in as_completed(futs):
                    ds = futs[fut]
                    try:
                        _, audits[ds] = fut.result()
                    except Exception as e:  # noqa: BLE001
                        errors[ds] = str(e)[:300]
        if len(datasets) > max_tables:
            for ds in datasets[max_tables:]:
                errors[ds] = (f"skipped — schema audit is capped at "
                              f"{max_tables} tables per run")

        from ..report import build_schema_report_data
        schema = (self._sources.get(source) or {}).get("params", {}).get("schema")
        data = build_schema_report_data(source, audits, errors, schema=schema)
        self._last_schema_audits[source] = data
        return data

    def report_schema(self, source: str, fmt: str = "docx",
                      out_dir: Optional[str] = None,
                      use_ai: bool = True) -> str:
        """Render the latest schema audit (or run it) to DOCX/PDF. Returns path."""
        from ..report import render_report
        data = (self._last_schema_audits.get(source)
                or self.audit_schema(source, use_ai=use_ai))
        out = Path(out_dir) if out_dir else Path(self.cfg.app_dir) / "reports"
        out.mkdir(parents=True, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in source)
        stamp = data["generated_at"][:19].replace(":", "-").replace("T", "_")
        path = str(out / f"datavigil_schema_report_{safe}_{stamp}.{fmt.lower()}")
        return render_report(data, path, fmt)

    # -- persistence of registered sources + rulesets ----------------------
    def _state_path(self) -> Path:
        return Path(self.cfg.app_dir) / "state.json"

    def _save_state(self) -> None:
        state = {
            "sources": self._sources,
            "rulesets": {
                k: [to_jsonable(r) for r in v] for k, v in self._rulesets.items()
            },
            "mappings": {
                k: to_jsonable(m) for k, m in self._mappings.items()
            },
        }
        self._state_path().write_text(json.dumps(state, indent=2, default=str))

    def _load_state(self) -> None:
        p = self._state_path()
        if not p.exists():
            return
        try:
            state = json.loads(p.read_text())
        except Exception:
            return
        self._sources = state.get("sources", {})
        for k, rules in state.get("rulesets", {}).items():
            self._rulesets[k] = [Rule(**{kk: vv for kk, vv in r.items()
                                         if kk in Rule.__dataclass_fields__})
                                 for r in rules]
        for k, m in state.get("mappings", {}).items():
            try:
                self._mappings[k] = DatasetMapping(
                    **{kk: vv for kk, vv in m.items()
                       if kk in DatasetMapping.__dataclass_fields__})
            except Exception:
                continue

    # -- batch ---------------------------------------------------------------
    def run_batch(self, manifest, parallelism: Optional[int] = None,
                  fail_fast: bool = False) -> dict:
        """Run a batch manifest (path/dict) — see core.batch for the format."""
        from .batch import load_manifest, run_batch
        if isinstance(manifest, (str, Path)):
            manifest = load_manifest(str(manifest))
        return run_batch(self, manifest, parallelism=parallelism,
                         fail_fast=fail_fast)
