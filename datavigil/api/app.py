"""FastAPI backend exposing all DataVigil engines + serving the dashboard."""
from __future__ import annotations

from pathlib import Path
from typing import Optional, Union

from fastapi import FastAPI, HTTPException, UploadFile, File, Form
from fastapi.responses import HTMLResponse, JSONResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from ..core.config import Config
from ..core.service import DataVigilService
from ..core.models import DatasetMapping, Rule, to_jsonable
from ..llm import LLMUnavailable

WEB_DIR = Path(__file__).resolve().parent.parent / "web"
UPLOAD_DIR = Path.home() / ".datavigil" / "uploads"


# Request models live at module scope: `from __future__ import annotations`
# stringifies endpoint annotations, and pydantic can only resolve names that
# exist in the module namespace.
class SourceReq(BaseModel):
    name: str
    type: str
    params: dict = {}


class RulesReq(BaseModel):
    source: str
    dataset: str
    rules: list[dict]


class NLReq(BaseModel):
    source: str
    dataset: str
    request: str


class AskReq(BaseModel):
    source: str
    dataset: str
    question: str


class MappingReq(BaseModel):
    name: str
    spec: dict                       # full DatasetMapping dict


class ReconcileReq(BaseModel):
    mapping: Union[str, dict]        # saved mapping name or inline spec


class BatchReq(BaseModel):
    manifest: dict
    parallelism: Optional[int] = None
    fail_fast: bool = False


class LLMCfgReq(BaseModel):
    provider: Optional[str] = None
    model: Optional[str] = None
    base_url: Optional[str] = None
    api_key: Optional[str] = None
    region: Optional[str] = None
    aws_profile: Optional[str] = None
    aws_auth: Optional[str] = None
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_session_token: Optional[str] = None
    bedrock_api_key: Optional[str] = None
    temperature: Optional[float] = None
    enabled: Optional[bool] = None


def create_app(cfg: Optional[Config] = None) -> FastAPI:
    cfg = cfg or Config.load()
    svc = DataVigilService(cfg)
    UPLOAD_DIR.mkdir(parents=True, exist_ok=True)

    app = FastAPI(title="DataVigil", version="0.1.0",
                  description="Independent data quality & profiling platform")

    if (WEB_DIR / "static").exists():
        app.mount("/static", StaticFiles(directory=WEB_DIR / "static"), name="static")

    # ---- dashboard ----
    @app.get("/", response_class=HTMLResponse)
    def index():
        html = (WEB_DIR / "templates" / "index.html")
        return html.read_text(encoding="utf-8") if html.exists() else "<h1>DataVigil</h1>"

    @app.get("/api/health")
    def health():
        try:
            from ..connectors.mcp_connector import MCPPostgresConnector
            mcp_ok = MCPPostgresConnector.available()
        except Exception:
            mcp_ok = False
        return {"status": "ok", "version": "0.1.0",
                "llm": svc.llm_health(), "mcp_available": mcp_ok}

    # ---- LLM settings (so non-technical users don't edit env files) ----
    @app.get("/api/llm/config")
    def llm_config():
        c = svc.cfg.llm
        return {"provider": c.provider, "model": c.model, "base_url": c.base_url,
                "region": c.region, "aws_profile": c.aws_profile,
                "aws_auth": c.aws_auth,
                "temperature": c.temperature, "enabled": c.enabled,
                "has_api_key": bool(c.api_key),
                # booleans only — secret values are never sent to the UI
                "has_aws_access_key": bool(c.aws_access_key_id),
                "has_aws_secret_key": bool(c.aws_secret_access_key),
                "has_aws_session_token": bool(c.aws_session_token),
                "has_bedrock_api_key": bool(c.bedrock_api_key)}

    @app.post("/api/llm/config")
    def set_llm_config(req: LLMCfgReq):
        try:
            updates = {k: v for k, v in req.model_dump().items() if v is not None}
            return svc.update_llm(**updates)
        except Exception as e:
            raise HTTPException(400, str(e))

    # ---- sources ----
    @app.get("/api/sources")
    def list_sources():
        return svc.list_sources()

    @app.post("/api/sources")
    def register_source(req: SourceReq):
        try:
            return svc.register_source(req.name, req.type, **req.params)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/upload")
    async def upload(file: UploadFile = File(...), name: str = Form(None),
                     options: str = Form(None)):
        dest = UPLOAD_DIR / file.filename
        dest.write_bytes(await file.read())
        src_name = name or Path(file.filename).stem
        reader_opts = {}
        if options:
            import json as _json
            try:
                reader_opts = _json.loads(options)
            except Exception:
                raise HTTPException(400, f"options is not valid JSON: {options!r}")
        info = svc.register_source(src_name, "file", path=str(dest),
                                   **reader_opts)
        return info

    @app.delete("/api/sources/{name}")
    def delete_source(name: str):
        info = svc.delete_source(name)
        if info is None:
            raise HTTPException(404, f"Source '{name}' not found")
        # uploaded copies live under UPLOAD_DIR — remove them too;
        # path-registered files belong to the user and are never touched
        if info.get("type") == "file":
            try:
                p = Path(info.get("params", {}).get("path", ""))
                if p.resolve().is_relative_to(UPLOAD_DIR.resolve()) and p.is_file():
                    p.unlink()
            except Exception:
                pass
        return {"deleted": name}

    @app.get("/api/sources/{source}/datasets")
    def datasets(source: str):
        try:
            return {"datasets": svc.list_datasets(source)}
        except Exception as e:
            raise HTTPException(404, str(e))

    # ---- profiling ----
    @app.get("/api/profile")
    def profile(source: str, dataset: str, insights: bool = False):
        try:
            prof = svc.profile(source, dataset, with_insights=insights)
            return to_jsonable(prof)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/api/compare")
    def compare(source_a: str, dataset_a: str, source_b: str, dataset_b: str):
        try:
            return svc.compare(source_a, dataset_a, source_b, dataset_b)
        except Exception as e:
            raise HTTPException(400, str(e))

    # ---- mappings / reconciliation --------------------------------------
    @app.get("/api/mappings")
    def list_mappings():
        return svc.list_mappings()

    @app.get("/api/mappings/suggest")
    def suggest_mapping(source_a: str, dataset_a: str,
                        source_b: str, dataset_b: str):
        try:
            return svc.suggest_mapping(source_a, dataset_a,
                                       source_b, dataset_b)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.put("/api/mappings/{name}")
    def put_mapping(name: str, spec: dict):
        try:
            spec = dict(spec, name=name)
            mp = DatasetMapping(**{k: v for k, v in spec.items()
                                   if k in DatasetMapping.__dataclass_fields__})
            svc.set_mapping(mp)
            return to_jsonable(mp)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.delete("/api/mappings/{name}")
    def delete_mapping(name: str):
        if not svc.delete_mapping(name):
            raise HTTPException(404, f"Mapping '{name}' not found")
        return {"deleted": name}

    @app.post("/api/reconcile")
    def reconcile_ep(req: ReconcileReq):
        try:
            return svc.reconcile(req.mapping)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except Exception as e:
            raise HTTPException(400, str(e))

    # ---- schema contracts (baseline / gate) ------------------------------
    @app.post("/api/schema/pin")
    def schema_pin(source: str, dataset: str):
        try:
            return svc.pin_schema(source, dataset)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/api/schema/versions")
    def schema_versions(source: str, dataset: str):
        return svc.schema_versions(source, dataset)

    @app.get("/api/schema/diff")
    def schema_diff(source: str, dataset: str,
                    version: Optional[int] = None):
        try:
            return svc.schema_diff(source, dataset, version=version)
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.post("/api/schema/gate")
    def schema_gate(source: str, dataset: str,
                    version: Optional[int] = None,
                    pin_if_missing: bool = False):
        try:
            return svc.schema_gate(source, dataset, version=version,
                                   pin_if_missing=pin_if_missing)
        except Exception as e:
            raise HTTPException(400, str(e))

    # ---- batch manifests ---------------------------------------------------
    @app.post("/api/batch")
    def batch(req: BatchReq):
        try:
            return svc.run_batch(req.manifest, parallelism=req.parallelism,
                                 fail_fast=req.fail_fast)
        except Exception as e:
            raise HTTPException(400, str(e))

    # ---- rules ----
    @app.get("/api/rule-types")
    def rule_types():
        from ..rules import list_rule_types
        return list_rule_types()

    @app.get("/api/rules")
    def get_rules(source: str, dataset: str):
        return [to_jsonable(r) for r in svc.get_rules(source, dataset)]

    @app.post("/api/rules/suggest")
    def suggest(source: str, dataset: str):
        return [to_jsonable(r) for r in svc.suggest(source, dataset)]

    @app.put("/api/rules")
    def set_rules(req: RulesReq):
        rules = [Rule(**{k: v for k, v in r.items()
                         if k in Rule.__dataclass_fields__}) for r in req.rules]
        svc.set_rules(req.source, req.dataset, rules)
        return {"count": len(rules)}

    @app.post("/api/rules/nl")
    def nl_rules(req: NLReq):
        try:
            rules = svc.nl_add_rules(req.source, req.dataset, req.request)
            return {"rules": [to_jsonable(r) for r in rules]}
        except LLMUnavailable as e:
            raise HTTPException(503, str(e))
        except Exception as e:
            raise HTTPException(400, str(e))

    # ---- run / score ----
    @app.post("/api/run")
    def run(source: str, dataset: str):
        try:
            return to_jsonable(svc.run(source, dataset))
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/api/history")
    def history(source: Optional[str] = None, dataset: Optional[str] = None):
        return svc.history(source, dataset)

    # ---- anomalies ----
    @app.get("/api/anomalies")
    def anomalies(source: str, dataset: str, methods: Optional[str] = None):
        try:
            m = methods.split(",") if methods else None
            return to_jsonable(svc.anomalies(source, dataset, methods=m))
        except Exception as e:
            raise HTTPException(400, str(e))

    # ---- AI root-cause analysis ----
    @app.post("/api/analyze")
    def analyze(source: str, dataset: str):
        try:
            return svc.analyze(source, dataset)
        except Exception as e:
            raise HTTPException(400, str(e))

    # ---- one-click audit + report export ----
    @app.post("/api/audit")
    def audit(source: str, dataset: str, use_ai: bool = True):
        try:
            return to_jsonable(svc.audit(source, dataset, use_ai=use_ai))
        except Exception as e:
            raise HTTPException(400, str(e))

    _MIME = {
        "docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "pdf": "application/pdf",
    }

    @app.get("/api/report")
    def report(source: str, dataset: str, format: str = "docx",
               use_ai: bool = True):
        fmt = format.lower()
        if fmt not in _MIME:
            raise HTTPException(400, f"Unsupported format '{format}' — use docx or pdf")
        try:
            path = svc.report(source, dataset, fmt=fmt, use_ai=use_ai)
        except Exception as e:
            raise HTTPException(400, str(e))
        return FileResponse(path, filename=Path(path).name, media_type=_MIME[fmt])

    # ---- whole-schema audit (all tables in the source, one click) ----
    @app.post("/api/audit/schema")
    def audit_schema(source: str, use_ai: bool = True, max_tables: int = 50):
        try:
            data = svc.audit_schema(source, use_ai=use_ai,
                                    max_tables=max_tables)
            # slim the response — full per-table bundles stay server-side
            # for report rendering
            return to_jsonable({k: v for k, v in data.items() if k != "audits"})
        except Exception as e:
            raise HTTPException(400, str(e))

    @app.get("/api/report/schema")
    def report_schema(source: str, format: str = "docx",
                      use_ai: bool = True):
        fmt = format.lower()
        if fmt not in _MIME:
            raise HTTPException(400, f"Unsupported format '{format}' — use docx or pdf")
        try:
            path = svc.report_schema(source, fmt=fmt, use_ai=use_ai)
        except Exception as e:
            raise HTTPException(400, str(e))
        return FileResponse(path, filename=Path(path).name, media_type=_MIME[fmt])

    # ---- conversational ----
    @app.post("/api/ask")
    def ask_q(req: AskReq):
        try:
            return {"answer": svc.ask(req.source, req.dataset, req.question)}
        except LLMUnavailable as e:
            raise HTTPException(503, str(e))
        except Exception as e:
            raise HTTPException(400, str(e))

    return app


app = create_app()
