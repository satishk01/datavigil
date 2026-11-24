"""Aurora PostgreSQL connector that goes through the AWS Labs
`postgres-mcp-server` (https://github.com/awslabs/mcp) over stdio.

Two connection methods (tool arg `method`, default 'rdsapi'):

  rdsapi   RDS Data API — HTTPS calls to AWS, so NO network path/VPC access
           to the cluster is needed. Works on provisioned AND serverless
           Aurora PostgreSQL, but requires:
             * RDS Data API enabled on the cluster
               (aws rds enable-http-endpoint --resource-arn <cluster-arn>)
             * a supported engine version (Aurora PG >= 13.11 / 14.8 / 15.3 /
               16.1 / 17.4 / 18.3)
             * IAM: rds-data:ExecuteStatement + secretsmanager:GetSecretValue
           If Data API is off, use 'pgwire' or the direct 'aurora' source
           instead.

  pgwire   Same direct Postgres wire connection DataVigil's SQLAlchemy connector
           makes, but brokered by the MCP server. Credentials come from the
           Secrets Manager ARN (needs db_endpoint/host + port).

Either way the tester only runs queries through a managed, read-only MCP
tool — no hand-written text-to-SQL is involved.

Requirements (only needed when this connector is used):
  pip install mcp
  plus a way to launch the server — `uv` (provides `uvx`), or
  `pip install awslabs.postgres-mcp-server`, or a custom `command`/`args`.

Params (env fallbacks):
  cluster        AURORA_CLUSTER_ARN / AWS_RESOURCE_ARN  cluster ARN **or** identifier
  secret_arn     AURORA_SECRET_ARN                      Secrets Manager ARN (DB creds)
  database       AURORA_DATABASE                        database name
  region         AWS_REGION / AWS_DEFAULT_REGION        AWS region
  method         'rdsapi' (default) | 'pgwire' | 'pgwire_iam'
  db_endpoint    cluster/instance endpoint (pgwire methods)
  port           5432 (pgwire methods)
  schema         public (default)
  aws_profile    AWS_PROFILE for the server's boto3 session
  command/args   override the server launch entirely
  max_rows       safety cap for dataset loads (default 10000)
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import threading
from typing import Any, Optional

import pandas as pd

from .base import Connector

_PKG_SPEC = "awslabs.postgres-mcp-server@latest"
_CONSOLE_SCRIPT = "awslabs.postgres-mcp-server"
_MODULE = "awslabs.postgres_mcp_server"
# no __main__.py in the package — entry point is server:main
_MODULE_RUN = "from awslabs.postgres_mcp_server.server import main; main()"


def _find_console_script() -> Optional[str]:
    """Console script on PATH, else next to sys.executable (Scripts dir may
    not be on PATH on Windows)."""
    if p := shutil.which(_CONSOLE_SCRIPT):
        return p
    scripts = os.path.join(os.path.dirname(sys.executable), "Scripts")
    for name in (_CONSOLE_SCRIPT, _CONSOLE_SCRIPT + ".exe"):
        cand = os.path.join(scripts, name)
        if os.path.isfile(cand):
            return cand
    return None


def _cluster_identifier(value: str) -> str:
    """Accept a cluster ARN or a bare identifier and return the identifier."""
    v = value.strip()
    if v.startswith("arn:"):
        return v.rsplit(":", 1)[-1].rsplit("/", 1)[-1]
    return v


class _PersistentSession:
    """Holds one stdio MCP session alive on a background event-loop thread."""

    def __init__(self, command: str, args: list[str], env: dict):
        try:
            from mcp import ClientSession, StdioServerParameters  # type: ignore
            from mcp.client.stdio import stdio_client  # type: ignore
        except ImportError as e:
            raise RuntimeError(
                "The 'mcp' package is required for the MCP connector — "
                "pip install mcp (or pip install datavigil[mcp])") from e
        self._params = StdioServerParameters(command=command, args=args, env=env)
        self._stdio_client = stdio_client
        self._client_session_cls = ClientSession
        self._loop = asyncio.new_event_loop()
        self._ready = threading.Event()
        self._err: Optional[BaseException] = None
        self.session = None
        self._cm = None
        self._sess_cm = None
        self._stop = None
        self._thread = threading.Thread(target=self._thread_main, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=60):
            self.close()
            raise RuntimeError("Timed out starting the Aurora MCP server")
        if self._err:
            self.close()
            raise RuntimeError(
                f"MCP server failed to start: {self._err}. The upstream "
                "server validates credentials at launch — check AWS "
                "credentials (aws sso login / env vars) and the Secrets "
                "Manager ARN, then retry.")

    def _thread_main(self):
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._main())

    async def _main(self):
        try:
            self._cm = self._stdio_client(self._params)
            read, write = await self._cm.__aenter__()
            self._sess_cm = self._client_session_cls(read, write)
            self.session = await self._sess_cm.__aenter__()
            await self.session.initialize()
        except BaseException as e:  # noqa: BLE001 — surfaced to caller thread
            self._err = e
            self._ready.set()
            return
        self._stop = asyncio.Event()
        self._ready.set()
        try:
            await self._stop.wait()      # park until close() sets it
        finally:
            for cm in (self._sess_cm, self._cm):
                if cm is not None:
                    try:
                        await cm.__aexit__(None, None, None)
                    except Exception:
                        pass

    def call(self, tool: str, arguments: dict, timeout: float = 120.0):
        fut = asyncio.run_coroutine_threadsafe(
            self.session.call_tool(tool, arguments), self._loop)
        return fut.result(timeout=timeout)

    def close(self):
        try:
            if self._loop.is_running() and self._stop is not None:
                self._loop.call_soon_threadsafe(self._stop.set)
                self._thread.join(timeout=10)
        except Exception:
            pass


class MCPPostgresConnector(Connector):
    kind = "mcp"

    def __init__(self, cluster: Optional[str] = None,
                 cluster_arn: Optional[str] = None,
                 secret_arn: Optional[str] = None,
                 database: Optional[str] = None,
                 region: Optional[str] = None,
                 method: str = "rdsapi",
                 db_endpoint: Optional[str] = None,
                 port: Optional[int] = None,
                 schema: Optional[str] = None,
                 aws_profile: Optional[str] = None,
                 command: Optional[str] = None,
                 args: Optional[list[str]] = None,
                 max_rows: int = 10_000):
        self.cluster_identifier = _cluster_identifier(
            cluster or cluster_arn or os.environ.get("AURORA_CLUSTER_ARN")
            or os.environ.get("AWS_RESOURCE_ARN") or "")
        self.secret_arn = secret_arn or os.environ.get("AURORA_SECRET_ARN")
        self.database = database or os.environ.get("AURORA_DATABASE")
        self.region = (region or os.environ.get("AWS_REGION")
                       or os.environ.get("AWS_DEFAULT_REGION") or "us-east-1")
        self.method = (method or "rdsapi").lower()
        self.db_endpoint = db_endpoint or ""
        self.port = int(port or 5432)
        self.schema = schema or "public"
        self.aws_profile = aws_profile or os.environ.get("AWS_PROFILE")
        self.max_rows = int(max_rows or 10_000)

        if not self.cluster_identifier:
            raise ValueError("MCP Aurora connection needs the cluster "
                             "identifier or ARN (or AURORA_CLUSTER_ARN).")
        if self.method == "rdsapi" and not (self.secret_arn or args):
            raise ValueError(
                "RDS Data API needs a Secrets Manager ARN with the DB "
                "credentials — pass secret_arn (or AURORA_SECRET_ARN).")
        if not self.database:
            raise ValueError("MCP Aurora connection needs a database name "
                             "(or AURORA_DATABASE).")
        if self.method in ("pgwire", "pgwire_iam") and not self.db_endpoint:
            raise ValueError(f"method='{self.method}' needs db_endpoint "
                             "(the cluster or instance host).")

        if args:
            self.command, self.args = command or "uvx", list(args)
        else:
            tail: list[str] = []
            if self.secret_arn:
                tail += ["--secret_arn", self.secret_arn]
            if command:
                self.command = command
                pkg = [_PKG_SPEC] if os.path.basename(command) == "uvx" else []
                self.args = pkg + tail
            elif shutil.which("uvx"):
                self.command, self.args = "uvx", [_PKG_SPEC] + tail
            elif (script := _find_console_script()):
                self.command, self.args = script, tail
            elif importlib.util.find_spec(_MODULE) is not None:
                self.command, self.args = sys.executable, ["-c", _MODULE_RUN] + tail
            else:
                raise RuntimeError(
                    "Cannot launch the Aurora MCP server — install one of: "
                    "'uv' (gives uvx), or 'pip install awslabs.postgres-mcp-server'.")

        self._session: Optional[_PersistentSession] = None
        self._conn_key: Optional[dict] = None      # resolved connect params
        self._tool_names: Optional[list[str]] = None

    # -- session + connection ----------------------------------------------
    def _env(self) -> dict:
        env = dict(os.environ)
        env["FASTMCP_LOG_LEVEL"] = "ERROR"
        env["AWS_REGION"] = self.region
        if self.aws_profile:
            env["AWS_PROFILE"] = self.aws_profile
        return env

    def _ensure(self):
        if self._session is None:
            self._session = _PersistentSession(self.command, self.args, self._env())
            self._connect()
        return self._session

    def _call(self, tool: str, arguments: dict):
        try:
            result = self._ensure().call(tool, arguments)
        except Exception:
            # server died (broken pipe / timeout) — respawn once, then fail
            self.close()
            result = self._ensure().call(tool, arguments)
        # mcp 1.x: isError attr; mcp 2.x: is_error (camelCase is a wire alias)
        if getattr(result, "is_error", getattr(result, "isError", False)):
            text = " ".join(getattr(c, "text", "") for c in result.content)
            raise RuntimeError(f"MCP '{tool}' failed: {text[:400]}")
        return result

    @staticmethod
    def _text(result) -> str:
        return " ".join(getattr(c, "text", "") for c in result.content or [])

    def _connect(self):
        """Call connect_to_database + record the exact run_query key params."""
        names = self._list_tools()
        if "connect_to_database" in names:
            res = self._call("connect_to_database", {
                "region": self.region,
                "database_type": "APG",
                "connection_method": self.method,
                "cluster_identifier": self.cluster_identifier,
                "db_endpoint": self.db_endpoint,
                "port": self.port,
                "database": self.database,
            })
            text = self._text(res)
            if '"error"' in text.lower() or "failed" in text.lower():
                raise RuntimeError(f"Could not connect via MCP: {text[:400]}")
        self._conn_key = self._resolve_key()

    def _list_tools(self) -> list[str]:
        if self._tool_names is None:
            sess = self._ensure()
            fut = asyncio.run_coroutine_threadsafe(
                sess.session.list_tools(), sess._loop)
            self._tool_names = [t.name for t in fut.result(timeout=30).tools]
        return self._tool_names

    def _resolve_key(self) -> dict:
        """Exact connection-map key for run_query — ask the server itself."""
        key = {"connection_method": self.method,
               "cluster_identifier": self.cluster_identifier,
               "db_endpoint": self.db_endpoint,
               "database": self.database}
        if "get_database_connection_info" in self._list_tools():
            try:
                info = self._text(self._call("get_database_connection_info", {}))
                entries = json.loads(info)
                for e in entries:
                    if (e.get("connection_method") == self.method
                            and e.get("database") == self.database
                            and (not e.get("cluster_identifier")
                                 or e["cluster_identifier"] == self.cluster_identifier)):
                        key["db_endpoint"] = e.get("db_endpoint", self.db_endpoint)
                        key["cluster_identifier"] = e.get(
                            "cluster_identifier", self.cluster_identifier)
                        break
            except Exception:
                pass  # fall back to what we sent
        return key

    # -- queries -------------------------------------------------------------
    def _query(self, sql: str) -> Any:
        names = self._list_tools()
        tool = next((n for n in ("run_query", "query", "execute_query")
                     if n in names), None)
        if not tool:
            raise RuntimeError(
                "MCP server has no query tool. This connector targets "
                "awslabs.postgres-mcp-server >= 1.0 — please upgrade.")
        res = self._call(tool, {"sql": sql, **self._conn_key})
        text = self._text(res)
        payload = getattr(res, "structured_content",
                          getattr(res, "structuredContent", None))
        if payload is not None:
            if isinstance(payload, dict) and "result" in payload:
                payload = payload["result"]
            return payload
        try:
            return json.loads(text)
        except (json.JSONDecodeError, TypeError):
            return [{"error": text}]

    # -- result parsing ------------------------------------------------------
    @staticmethod
    def _records(payload: Any) -> list[dict]:
        """Normalise run_query output into a list of row dicts."""
        if isinstance(payload, str):
            try:
                payload = json.loads(payload)
            except json.JSONDecodeError:
                return []
        if isinstance(payload, list) and payload and isinstance(payload[0], dict) \
                and "error" in payload[0] and len(payload[0]) == 1:
            raise RuntimeError(f"MCP query error: {payload[0]['error']}")
        # RDS Data API raw shape: {columnMetadata: [{name}], records: [[..]]}
        if isinstance(payload, dict) and "columnMetadata" in payload:
            names = [c.get("name", f"c{i}") for i, c in
                     enumerate(payload["columnMetadata"])]
            return [dict(zip(names, row)) for row in payload.get("records", [])]
        if isinstance(payload, dict):
            for key in ("records", "rows", "data", "result", "results"):
                rows = payload.get(key)
                if isinstance(rows, list):
                    return [r if isinstance(r, dict) else {"value": r} for r in rows]
            return [payload]
        if isinstance(payload, list):
            return [r if isinstance(r, dict) else {"value": r} for r in payload]
        return []

    @staticmethod
    def _qident(name: str) -> str:
        return '"' + name.replace('"', '""') + '"'

    def _qualified(self, dataset: str) -> str:
        if "." in dataset:
            return ".".join(self._qident(p) for p in dataset.split(".", 1))
        return f"{self._qident(self.schema)}.{self._qident(dataset)}"

    # -- Connector interface -------------------------------------------------
    def list_datasets(self) -> list[str]:
        sql = (
            "SELECT table_name FROM information_schema.tables "
            f"WHERE table_schema = '{self.schema}' "
            "AND table_type IN ('BASE TABLE','VIEW') ORDER BY table_name")
        rows = self._records(self._query(sql))
        return [str(next(iter(r.values()))) for r in rows]

    def load(self, dataset: str, limit: Optional[int] = None) -> pd.DataFrame:
        n = int(limit) if limit else self.max_rows
        sql = f"SELECT * FROM {self._qualified(dataset)} LIMIT {n}"
        return pd.DataFrame(self._records(self._query(sql)))

    def row_count(self, dataset: str) -> Optional[int]:
        sql = f"SELECT COUNT(*) AS n FROM {self._qualified(dataset)}"
        try:
            rows = self._records(self._query(sql))
            if rows:
                return int(next(iter(rows[0].values())))
        except Exception:
            return None
        return None

    def close(self) -> None:
        if self._session is not None:
            self._session.close()
            self._session = None
            self._conn_key = None

    @staticmethod
    def available() -> bool:
        """True if prerequisites are installed (mcp pkg + a way to launch the server)."""
        try:
            import mcp  # type: ignore  # noqa: F401
        except ImportError:
            return False
        return (shutil.which("uvx") is not None
                or _find_console_script() is not None
                or importlib.util.find_spec(_MODULE) is not None)
