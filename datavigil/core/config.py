"""Configuration for DataVigil.

Loads from (in order of precedence):
  1. explicit kwargs
  2. environment variables (DATAVIGIL_*)
  3. a config.yaml / config.json file if present
  4. built-in defaults

Kept dependency-light: YAML is optional.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any, Optional

APP_DIR = Path(os.environ.get("DATAVIGIL_HOME", Path.home() / ".datavigil"))
DATA_DIR = APP_DIR / "data"
RUNS_DIR = APP_DIR / "runs"


@dataclass
class LLMConfig:
    provider: str = "ollama"              # ollama | openai | azure_openai | bedrock | gemini | none
    model: str = "llama3.1"
    base_url: str = "http://localhost:11434"
    api_key: Optional[str] = None
    temperature: float = 0.1
    timeout: int = 120
    enabled: bool = True
    region: Optional[str] = None          # AWS region (bedrock) — falls back to AWS_REGION/AWS_DEFAULT_REGION
    aws_profile: Optional[str] = None     # AWS credential profile (bedrock / aurora IAM)
    aws_auth: str = "profile"             # bedrock auth: profile | keys | keys_session | api_key
    aws_access_key_id: Optional[str] = None
    aws_secret_access_key: Optional[str] = None
    aws_session_token: Optional[str] = None
    bedrock_api_key: Optional[str] = None  # bearer-token Bedrock API key (AWS_BEARER_TOKEN_BEDROCK)


@dataclass
class Config:
    llm: LLMConfig = field(default_factory=LLMConfig)
    app_dir: str = str(APP_DIR)
    max_profile_sample: int = 500_000       # cap rows pulled for profiling
    max_distinct_tracked: int = 50          # top-N value tracking
    pattern_sample: int = 10_000            # rows sampled for pattern inference
    correlation_threshold: float = 0.5      # only surface |r| >= this
    anomaly_contamination: float = 0.02     # expected outlier fraction
    parallel_workers: int = 4               # threads for schema audit / batch
    audit_max_tables: int = 50              # whole-schema audit table cap

    @classmethod
    def load(cls, path: Optional[str] = None, **overrides: Any) -> "Config":
        data: dict = {}

        # file: explicit path > env > saved config in the app dir
        candidates = []
        if path:
            candidates.append(path)
        elif os.environ.get("DATAVIGIL_CONFIG"):
            candidates.append(os.environ["DATAVIGIL_CONFIG"])
        else:
            candidates += [str(APP_DIR / "config.yaml"), str(APP_DIR / "config.json")]
        candidate = next((c for c in candidates if Path(c).exists()), None)
        if candidate:
            text = Path(candidate).read_text()
            if candidate.endswith((".yaml", ".yml")):
                try:
                    import yaml  # type: ignore
                    data = yaml.safe_load(text) or {}
                except ImportError:
                    data = {}
            else:
                data = json.loads(text)

        cfg = cls()
        llm_data = data.pop("llm", {}) if isinstance(data, dict) else {}
        for k, v in (data or {}).items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
        for k, v in (llm_data or {}).items():
            if hasattr(cfg.llm, k):
                setattr(cfg.llm, k, v)

        # env overrides
        env_map = {
            "DATAVIGIL_LLM_PROVIDER": ("llm", "provider"),
            "DATAVIGIL_LLM_MODEL": ("llm", "model"),
            "DATAVIGIL_LLM_BASE_URL": ("llm", "base_url"),
            "DATAVIGIL_LLM_API_KEY": ("llm", "api_key"),
            "DATAVIGIL_LLM_REGION": ("llm", "region"),
            "DATAVIGIL_LLM_AWS_PROFILE": ("llm", "aws_profile"),
            "DATAVIGIL_LLM_AWS_AUTH": ("llm", "aws_auth"),
            "DATAVIGIL_LLM_AWS_ACCESS_KEY_ID": ("llm", "aws_access_key_id"),
            "DATAVIGIL_LLM_AWS_SECRET_ACCESS_KEY": ("llm", "aws_secret_access_key"),
            "DATAVIGIL_LLM_AWS_SESSION_TOKEN": ("llm", "aws_session_token"),
            "DATAVIGIL_LLM_BEDROCK_API_KEY": ("llm", "bedrock_api_key"),
        }
        for env, (section, attr) in env_map.items():
            val = os.environ.get(env)
            if val is not None:
                setattr(getattr(cfg, section), attr, val)

        # explicit overrides
        for k, v in overrides.items():
            if k == "llm" and isinstance(v, dict):
                for lk, lv in v.items():
                    if hasattr(cfg.llm, lk):
                        setattr(cfg.llm, lk, lv)
            elif hasattr(cfg, k):
                setattr(cfg, k, v)

        cfg._ensure_dirs()
        return cfg

    def _ensure_dirs(self) -> None:
        for d in (Path(self.app_dir), Path(self.app_dir) / "runs"):
            d.mkdir(parents=True, exist_ok=True)

    def save(self) -> Path:
        """Persist current settings to <app_dir>/config.yaml (picked up on next load)."""
        self._ensure_dirs()
        p = Path(self.app_dir) / "config.yaml"
        try:
            import yaml  # type: ignore
            p.write_text(yaml.safe_dump(self.to_dict(), sort_keys=False))
        except ImportError:
            (Path(self.app_dir) / "config.json").write_text(
                json.dumps(self.to_dict(), indent=2))
        return p

    def to_dict(self) -> dict:
        return asdict(self)
