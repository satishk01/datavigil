"""Pluggable LLM provider.

Supported providers:
  bedrock   AWS Bedrock via boto3 `converse` (Nova Pro/Lite/Micro, Claude,
            Llama, Mistral — any model id your account has access to).
            Credentials come from the standard AWS chain (env, ~/.aws,
            IAM role); a named profile / region can be set in config.
  ollama    Local Ollama server (default).
  openai / azure_openai   Chat-completions shape; custom base_url supported.
  none      Disable LLM features gracefully; everything else still works.
"""
from __future__ import annotations

import json
import os
from typing import Optional

import requests

from ..core.config import LLMConfig


class LLMUnavailable(RuntimeError):
    pass


class LLMProvider:
    def __init__(self, cfg: LLMConfig):
        self.cfg = cfg

    @classmethod
    def from_config(cls, cfg: LLMConfig) -> "LLMProvider":
        p = cfg.provider.lower()
        if p in ("bedrock", "aws_bedrock", "aws"):
            return BedrockProvider(cfg)
        if p == "ollama":
            return OllamaProvider(cfg)
        if p in ("openai", "azure_openai"):
            return OpenAIProvider(cfg)
        if p == "none":
            return NullProvider(cfg)
        # default to ollama shape
        return OllamaProvider(cfg)

    @property
    def available(self) -> bool:
        return self.cfg.enabled

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        raise NotImplementedError

    def health(self) -> dict:
        try:
            out = self.complete("You are a health check.", "Reply with OK.")
            return {"ok": True, "provider": self.cfg.provider, "model": self.cfg.model,
                    "sample": out[:40]}
        except Exception as e:
            return {"ok": False, "provider": self.cfg.provider, "error": str(e)}


class NullProvider(LLMProvider):
    @property
    def available(self) -> bool:
        return False

    def complete(self, system, user, json_mode=False):
        raise LLMUnavailable("LLM features are disabled (provider=none).")


class OllamaProvider(LLMProvider):
    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        url = f"{self.cfg.base_url.rstrip('/')}/api/chat"
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "stream": False,
            "options": {"temperature": self.cfg.temperature},
        }
        if json_mode:
            payload["format"] = "json"
        try:
            r = requests.post(url, json=payload, timeout=self.cfg.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise LLMUnavailable(f"Ollama request failed: {e}") from e
        return r.json().get("message", {}).get("content", "").strip()


class OpenAIProvider(LLMProvider):
    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        base = self.cfg.base_url.rstrip("/")
        if "openai.com" not in base and "/v1" not in base and self.cfg.provider == "openai":
            base = "https://api.openai.com/v1"
        url = f"{base}/chat/completions"
        headers = {"Authorization": f"Bearer {self.cfg.api_key or ''}",
                   "Content-Type": "application/json"}
        payload = {
            "model": self.cfg.model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "temperature": self.cfg.temperature,
        }
        if json_mode:
            payload["response_format"] = {"type": "json_object"}
        try:
            r = requests.post(url, headers=headers, json=payload, timeout=self.cfg.timeout)
            r.raise_for_status()
        except requests.RequestException as e:
            raise LLMUnavailable(f"OpenAI request failed: {e}") from e
        return r.json()["choices"][0]["message"]["content"].strip()


class BedrockProvider(LLMProvider):
    """AWS Bedrock via the Converse API — works with Nova, Claude, Llama, etc.

    Auth (cfg.aws_auth):
      profile      AWS profile / default credential chain (env vars, ~/.aws,
                   IAM role, SSO) — `aws_profile` optional
      keys         long-term access key + secret (session token optional)
      keys_session short-term access key + secret + session token (required)
      api_key      Bedrock API key — bearer token, no SigV4 creds needed
                   (botocore >= 1.38.27; read from AWS_BEARER_TOKEN_BEDROCK)

    Region: `region` config, else AWS_REGION / AWS_DEFAULT_REGION, else
    the session's. Default model suggestion: us.amazon.nova-pro-v1:0
    """

    _BEARER_ENV = "AWS_BEARER_TOKEN_BEDROCK"

    def __init__(self, cfg: LLMConfig):
        super().__init__(cfg)
        self._client = None
        self._set_bearer = False   # we put our key in the env — clean it up

    def _drop_bearer(self):
        """Remove the bearer env var only if it holds OUR configured key —
        leaves a value the user exported themselves untouched."""
        if os.environ.get(self._BEARER_ENV) == self.cfg.bedrock_api_key:
            os.environ.pop(self._BEARER_ENV, None)
            self._set_bearer = False

    def _get_client(self):
        if self._client is not None:
            return self._client
        try:
            import boto3  # type: ignore
        except ImportError as e:
            raise LLMUnavailable(
                "boto3 is required for the bedrock provider — "
                "pip install boto3 (or pip install datavigil[bedrock])") from e

        method = (self.cfg.aws_auth or "profile").lower()
        sess_kwargs: dict = {}

        if method == "api_key":
            if not self.cfg.bedrock_api_key:
                raise LLMUnavailable(
                    "Bedrock API-key auth selected but no key is set — "
                    "add it in AI model settings.")
            os.environ[self._BEARER_ENV] = self.cfg.bedrock_api_key
            self._set_bearer = True
        elif method in ("keys", "keys_session"):
            if not (self.cfg.aws_access_key_id and self.cfg.aws_secret_access_key):
                raise LLMUnavailable(
                    f"AWS {method} auth needs an access key ID and secret "
                    "access key — add them in AI model settings.")
            if method == "keys_session" and not self.cfg.aws_session_token:
                raise LLMUnavailable(
                    "Short-term AWS credentials need a session token too — "
                    "paste it in AI model settings.")
            sess_kwargs["aws_access_key_id"] = self.cfg.aws_access_key_id
            sess_kwargs["aws_secret_access_key"] = self.cfg.aws_secret_access_key
            if self.cfg.aws_session_token:
                sess_kwargs["aws_session_token"] = self.cfg.aws_session_token
            self._drop_bearer()
        else:  # profile / default chain
            if self.cfg.aws_profile:
                sess_kwargs["profile_name"] = self.cfg.aws_profile
            self._drop_bearer()

        try:
            session = boto3.Session(**sess_kwargs)
            region = (self.cfg.region or session.region_name
                      or os.environ.get("AWS_REGION")
                      or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
            self._client = session.client("bedrock-runtime", region_name=region)
        except Exception as e:
            raise LLMUnavailable(f"Bedrock client setup failed: {e}") from e
        return self._client

    def complete(self, system: str, user: str, json_mode: bool = False) -> str:
        client = self._get_client()
        sys_text = system
        if json_mode:
            sys_text += "\nRespond with valid JSON only — no markdown fences, no prose."
        try:
            resp = client.converse(
                modelId=self.cfg.model,
                system=[{"text": sys_text}],
                messages=[{"role": "user", "content": [{"text": user}]}],
                inferenceConfig={
                    "temperature": self.cfg.temperature,
                    "maxTokens": 4096,
                },
            )
        except Exception as e:
            raise LLMUnavailable(f"Bedrock request failed: {e}") from e
        parts = resp.get("output", {}).get("message", {}).get("content", [])
        return "".join(p.get("text", "") for p in parts).strip()


def extract_json(text: str) -> Optional[dict | list]:
    """Pull the first JSON object/array out of a model response."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    for open_c, close_c in (("{", "}"), ("[", "]")):
        start = text.find(open_c)
        end = text.rfind(close_c)
        if start != -1 and end != -1 and end > start:
            try:
                return json.loads(text[start:end + 1])
            except json.JSONDecodeError:
                continue
    return None
