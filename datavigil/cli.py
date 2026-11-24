"""DataVigil command-line interface (for CI / scheduled runs / headless use)."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

import typer

from .core.config import Config
from .core.service import DataVigilService
from .core.models import to_jsonable

app = typer.Typer(help="DataVigil — independent data quality & profiling platform", add_completion=False)


def _svc() -> DataVigilService:
    return DataVigilService(Config.load())


def _file_svc(path: str, options: Optional[str] = None):
    """Service with `path` registered as file source '_cli'."""
    svc = _svc()
    opts = json.loads(options) if options else {}
    svc.register_source("_cli", "file", path=path, **opts)
    return svc


@app.command()
def serve(host: str = "127.0.0.1", port: int = 8000, reload: bool = False):
    """Launch the web dashboard + API."""
    import uvicorn
    uvicorn.run("datavigil.api.app:app", host=host, port=port, reload=reload)


@app.command()
def profile(path: str, dataset: Optional[str] = None, insights: bool = False,
            out: Optional[str] = None,
            options: Optional[str] = typer.Option(
                None, help='JSON reader options, e.g. \'{"sep":"|"}\'')):
    """Profile a file or dataset."""
    svc = _file_svc(path, options)
    ds = dataset or svc.list_datasets("_cli")[0]
    prof = svc.profile("_cli", ds, with_insights=insights)
    _emit(to_jsonable(prof), out)


@app.command()
def run(path: str, dataset: Optional[str] = None, out: Optional[str] = None,
        fail_under: float = 0.0,
        options: Optional[str] = typer.Option(
            None, help='JSON reader options, e.g. \'{"sep":"|"}\'')):
    """Suggest + run rules and print a DQ score. Exit non-zero if score < fail-under."""
    svc = _file_svc(path, options)
    ds = dataset or svc.list_datasets("_cli")[0]
    result = svc.run("_cli", ds)
    _emit(to_jsonable(result), out)
    typer.secho(f"DQ score: {result.score}/100  "
                f"({result.passed_rules}/{result.total_rules} rules passed)",
                fg="green" if result.score >= 75 else "yellow")
    if fail_under and result.score < fail_under:
        typer.secho(f"Score {result.score} below threshold {fail_under}", fg="red")
        raise typer.Exit(code=1)


@app.command()
def anomalies(path: str, dataset: Optional[str] = None,
              methods: Optional[str] = None, out: Optional[str] = None,
              options: Optional[str] = typer.Option(
                  None, help='JSON reader options, e.g. \'{"sep":"|"}\'')):
    """Detect anomalies in a dataset."""
    svc = _file_svc(path, options)
    ds = dataset or svc.list_datasets("_cli")[0]
    m = methods.split(",") if methods else None
    res = svc.anomalies("_cli", ds, methods=m)
    _emit(to_jsonable(res), out)
    typer.echo(f"{res.anomaly_count} anomalies across {res.total_rows} rows")


@app.command()
def audit(path: str, dataset: Optional[str] = None, no_ai: bool = False,
          all_tables: bool = typer.Option(False, "--all",
                                          help="Audit every dataset/table in the source"),
          options: Optional[str] = typer.Option(
              None, help='JSON reader options, e.g. \'{"sep":"|"}\'')):
    """One-shot audit: profile + rules + anomalies + analysis for a file."""
    svc = _file_svc(path, options)
    if all_tables:
        data = svc.audit_schema("_cli", use_ai=not no_ai)
        s = data["summary"]
        typer.echo(f"Schema audit: {s['tables_audited']} tables  "
                   f"avg={s['avg_score']}/100  min={s['min_score']}/100  "
                   f"rules {s['total_failed_rules']}/{s['total_rules']} failed")
        for t in data["tables"][:15]:
            typer.echo(f"  {t['dataset']}: {t['score']}/100 "
                       f"({t['rules_failed']} failed rules)")
        return
    ds = dataset or svc.list_datasets("_cli")[0]
    data = svc.audit("_cli", ds, use_ai=not no_ai)
    run, prof, anom = data.get("run", {}), data.get("profile", {}), data.get("anomalies", {})
    typer.echo(f"Dataset: {data['dataset']}  rows={prof.get('row_count')}  "
               f"score={run.get('score')}/100  "
               f"rules {run.get('passed_rules')}/{run.get('total_rules')} passed  "
               f"anomalies={anom.get('count')}")
    typer.echo((data.get("analysis") or {}).get("summary", ""))
    for i in (data.get("analysis") or {}).get("issues", [])[:10]:
        typer.echo(f"  [{i.get('priority','')}] {i.get('rule','')}: "
                   f"{i.get('recommended_fix','')}")


@app.command()
def report(path: str, dataset: Optional[str] = None, format: str = "docx",
           out: Optional[str] = None, no_ai: bool = False,
           all_tables: bool = typer.Option(False, "--all",
                                           help="Audit + report every dataset in the source"),
           options: Optional[str] = typer.Option(
               None, help='JSON reader options, e.g. \'{"sep":"|"}\'')):
    """Audit a file and export a DOCX or PDF report."""
    svc = _file_svc(path, options)
    if all_tables:
        p = svc.report_schema("_cli", fmt=format, out_dir=out,
                              use_ai=not no_ai)
    else:
        ds = dataset or svc.list_datasets("_cli")[0]
        p = svc.report("_cli", ds, fmt=format, out_dir=out, use_ai=not no_ai)
    typer.secho(f"wrote {p}", fg="green")


# --------------------------------------------------------------------------
# schema contracts — pin a baseline, gate later loads against it
# --------------------------------------------------------------------------
schema_app = typer.Typer(help="Schema contracts: pin baselines, gate loads")
app.add_typer(schema_app, name="schema")


@schema_app.command("pin")
def schema_pin(path: str, dataset: Optional[str] = None,
               options: Optional[str] = None):
    """Pin the current schema as a new baseline version for a file."""
    svc = _file_svc(path, options)
    ds = dataset or svc.list_datasets("_cli")[0]
    entry = svc.pin_schema("_cli", ds)
    typer.secho(f"pinned {ds} as baseline v{entry['version']} "
                f"({len(entry['columns'])} columns)", fg="green")


@schema_app.command("versions")
def schema_versions(path: str, dataset: Optional[str] = None,
                    options: Optional[str] = None):
    """List pinned schema baseline versions for a file."""
    svc = _file_svc(path, options)
    ds = dataset or svc.list_datasets("_cli")[0]
    _emit(svc.schema_versions("_cli", ds), None)


@schema_app.command("diff")
def schema_diff(path: str, dataset: Optional[str] = None,
                version: Optional[int] = None,
                options: Optional[str] = None):
    """Diff a file's current schema against its pinned baseline."""
    svc = _file_svc(path, options)
    ds = dataset or svc.list_datasets("_cli")[0]
    _emit(svc.schema_diff("_cli", ds, version=version), None)


@app.command()
def gate(path: str, dataset: Optional[str] = None,
         version: Optional[int] = None,
         pin_if_missing: bool = False,
         options: Optional[str] = typer.Option(
             None, help='JSON reader options, e.g. \'{"sep":"|"}\'')):
    """Schema gate: fail (exit 1) if the file's schema breaks the baseline.

    Wire this into an ingestion script / ECS task before the load:
        datavigil schema pin landing/customers.csv      # once, v1 baseline
        datavigil gate landing/customers_v2.csv         # every batch
    """
    svc = _file_svc(path, options)
    ds = dataset or svc.list_datasets("_cli")[0]
    res = svc.schema_gate("_cli", ds, version=version,
                          pin_if_missing=pin_if_missing)
    for c in res.get("breaking", []):
        typer.secho(f"  BREAKING {c['kind']}: {c['column']} "
                    f"{c.get('from')} -> {c.get('to')}", fg="red")
    for c in res.get("warnings", []):
        typer.secho(f"  warning  {c['kind']}: {c['column']} "
                    f"{c.get('from')} -> {c.get('to')}", fg="yellow")
    for c in res.get("compatible", []):
        typer.echo(f"  ok       {c['kind']}: {c['column']} -> {c.get('to')}")
    typer.secho(res["message"], fg="green" if res["passed"] else "red")
    if not res["passed"]:
        raise typer.Exit(code=1)


@app.command()
def reconcile(mapping: str, name: Optional[str] = None,
              out: Optional[str] = None):
    """Run a saved or inline source->target reconciliation.

    `mapping` is either the name of a mapping saved in the UI/API, or a path
    to a JSON/YAML mapping spec. The spec may use `source_file`/`target_file`
    (paths) instead of registered source names.
    """
    svc = _svc()
    spec: object = mapping
    p = Path(mapping)
    if p.exists():
        text = p.read_text()
        if p.suffix.lower() in (".yaml", ".yml"):
            import yaml  # type: ignore
            spec = yaml.safe_load(text)
        else:
            spec = json.loads(text)
    if isinstance(spec, dict):
        spec = dict(spec)
        for side, fkey, skey, dkey in (
            ("a", "source_file", "source_a", "dataset_a"),
            ("b", "target_file", "source_b", "dataset_b"),
        ):
            if spec.get(fkey):
                sname = f"cli_{side}_{Path(spec[fkey]).stem}"
                svc.register_source(sname, "file", path=spec.pop(fkey))
                spec[skey] = sname
                spec.setdefault(dkey, svc.list_datasets(sname)[0])
    try:
        res = svc.reconcile(spec)
    except KeyError as e:
        typer.secho(str(e), fg="red")
        raise typer.Exit(code=2)
    _emit(res, out)
    s = res["summary"]
    typer.secho(
        f"Reconcile {res['mapping']}: {'PASS' if res['passed'] else 'FAIL'} — "
        f"{s['checks_passed']}/{s['checks_total']} checks passed",
        fg="green" if res["passed"] else "red")
    if not res["passed"]:
        raise typer.Exit(code=1)


@app.command()
def batch(manifest: str, parallel: Optional[int] = None,
          fail_fast: bool = False, out: Optional[str] = None):
    """Run a batch manifest (YAML/JSON): audits, reconciles, schema gates.

    Exit code is non-zero if any job fails — plug into CI / ECS scheduled
    tasks to break a batch on bad data.
    """
    svc = _svc()
    try:
        report = svc.run_batch(manifest, parallelism=parallel,
                               fail_fast=fail_fast)
    except Exception as e:
        typer.secho(f"batch failed: {e}", fg="red")
        raise typer.Exit(code=2)
    _emit(report, out)
    for r in report["results"]:
        mark = "PASS" if r["passed"] else "FAIL"
        extra = (f" score={r['score']}" if r.get("score") is not None
                 else f" {r.get('error', '')}"[:80])
        typer.secho(f"  [{mark}] {r['name']} ({r['type']}){extra}",
                    fg="green" if r["passed"] else "red")
    typer.secho(f"batch {'PASSED' if report['passed'] else 'FAILED'} — "
                f"{report['jobs_failed']}/{report['jobs_total']} jobs failed",
                fg="green" if report["passed"] else "red")
    if not report["passed"]:
        raise typer.Exit(code=1)


@app.command("llm-health")
def llm_health():
    """Check the configured LLM provider."""
    typer.echo(json.dumps(_svc().llm_health(), indent=2))


def _emit(obj, out: Optional[str]):
    text = json.dumps(obj, indent=2, default=str)
    if out:
        with open(out, "w") as f:
            f.write(text)
        typer.echo(f"wrote {out}")
    else:
        typer.echo(text)


if __name__ == "__main__":
    app()
