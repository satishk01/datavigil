"""Database connector via SQLAlchemy.

Works with any SQLAlchemy-supported engine. Convenience builders are
provided for the v1 target databases; others work by passing a full
SQLAlchemy URL.

    postgres  -> postgresql+psycopg2://user:pass@host:port/db
    mysql     -> mysql+pymysql://user:pass@host:port/db
    sqlserver -> mssql+pyodbc://user:pass@host:port/db?driver=ODBC+Driver+17+for+SQL+Server
    oracle    -> oracle+oracledb://user:pass@host:port/?service_name=svc
    snowflake -> snowflake://user:pass@account/db/schema?warehouse=wh
    sqlite    -> sqlite:///path/to.db
"""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus

import pandas as pd
from sqlalchemy import create_engine, inspect, text

from .base import Connector


def _truthy(v) -> bool:
    return str(v).strip().lower() in ("1", "true", "yes", "y", "on")


def _rds_iam_token(host: str, port: int, user: str, region: Optional[str]) -> str:
    """Generate an RDS IAM auth token (valid ~15 min) via boto3."""
    try:
        import boto3  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "boto3 is required for Aurora IAM authentication — pip install boto3") from e
    rds = boto3.client("rds", region_name=region or None)
    return rds.generate_db_auth_token(DBHostname=host, Port=port, DBUsername=user)

_DRIVERS = {
    "postgres": "postgresql+psycopg2",
    "postgresql": "postgresql+psycopg2",
    "aurora": "postgresql+psycopg2",            # Aurora PostgreSQL (RDS)
    "aurora-postgres": "postgresql+psycopg2",
    "aurora_postgres": "postgresql+psycopg2",
    "rds": "postgresql+psycopg2",
    "mysql": "mysql+pymysql",
    "mariadb": "mysql+pymysql",
    "sqlserver": "mssql+pyodbc",
    "mssql": "mssql+pyodbc",
    "oracle": "oracle+oracledb",
    "snowflake": "snowflake",
    "sqlite": "sqlite",
}

_AURORA = {"aurora", "aurora-postgres", "aurora_postgres", "rds"}


def build_url(
    dialect: str,
    *,
    host: str = "localhost",
    port: Optional[int] = None,
    user: str = "",
    password: str = "",
    database: str = "",
    extra: str = "",
) -> str:
    driver = _DRIVERS.get(dialect.lower(), dialect)
    if dialect.lower() == "sqlite":
        return f"sqlite:///{database}"
    auth = ""
    if user:
        auth = quote_plus(user)
        if password:
            auth += f":{quote_plus(password)}"
        auth += "@"
    netloc = host
    if port:
        netloc += f":{port}"
    url = f"{driver}://{auth}{netloc}/{database}"
    if extra:
        url += f"?{extra}"
    return url


class DBConnector(Connector):
    kind = "database"

    def __init__(self, url: str, schema: Optional[str] = None):
        self.url = url
        self.schema = schema
        self.engine = create_engine(url, pool_pre_ping=True)

    @classmethod
    def from_params(cls, dialect: str, **kwargs) -> "DBConnector":
        schema = kwargs.pop("schema", None)
        iam_auth = _truthy(kwargs.pop("iam_auth", False))
        region = kwargs.pop("region", None)

        if dialect.lower() in _AURORA:
            # Aurora PostgreSQL: default to SSL; optionally use IAM auth tokens.
            extra = kwargs.get("extra", "")
            if "sslmode" not in extra:
                kwargs["extra"] = (extra + "&" if extra else "") + "sslmode=require"
            if iam_auth:
                if kwargs.get("password"):
                    raise ValueError(
                        "IAM auth is enabled — leave the password empty; "
                        "a short-lived token is generated automatically.")
                kwargs["password"] = _rds_iam_token(
                    host=kwargs.get("host", "localhost"),
                    port=int(kwargs.get("port") or 5432),
                    user=kwargs.get("user", ""),
                    region=region,
                )
        return cls(build_url(dialect, **kwargs), schema=schema)

    def list_datasets(self) -> list[str]:
        insp = inspect(self.engine)
        tables = insp.get_table_names(schema=self.schema)
        try:
            tables += insp.get_view_names(schema=self.schema)
        except Exception:
            pass
        return sorted(set(tables))

    def _qualified(self, dataset: str) -> str:
        if self.schema and "." not in dataset:
            return f"{self.schema}.{dataset}"
        return dataset

    def load(self, dataset: str, limit: Optional[int] = None) -> pd.DataFrame:
        qualified = self._qualified(dataset)
        sql = f"SELECT * FROM {qualified}"
        if limit:
            # LIMIT syntax varies; SQLAlchemy can't rewrite a raw string, so
            # use pandas' chunk read as a portable cap.
            return next(pd.read_sql(text(sql), self.engine, chunksize=limit))
        return pd.read_sql(text(sql), self.engine)

    def row_count(self, dataset: str) -> Optional[int]:
        qualified = self._qualified(dataset)
        try:
            with self.engine.connect() as conn:
                r = conn.execute(text(f"SELECT COUNT(*) FROM {qualified}"))
                return int(r.scalar_one())
        except Exception:
            return None

    def query(self, sql: str) -> pd.DataFrame:
        """Run an arbitrary read query (used for custom-SQL rules)."""
        return pd.read_sql(text(sql), self.engine)

    def close(self) -> None:
        self.engine.dispose()
