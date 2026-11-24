"""Connector factory."""
from __future__ import annotations

from typing import Any

from .base import Connector
from .file_connector import FileConnector
from .db_connector import DBConnector, build_url


def get_connector(source_type: str, **kwargs: Any) -> Connector:
    """Build a connector.

    source_type:
        "file"                 -> FileConnector(path=...)
        "database"             -> DBConnector(url=...) or DBConnector.from_params(dialect=..., ...)
        "mcp" / "aurora-mcp"   -> MCPPostgresConnector (Aurora via RDS Data API
                                  through the awslabs postgres-mcp-server)
        a known dialect name   -> DBConnector.from_params(dialect=source_type, ...)
    """
    st = source_type.lower()
    if st == "file":
        params = {k: v for k, v in kwargs.items()}
        return FileConnector(params.pop("path"), **params)
    if st in ("copybook", "ims", "cobol"):
        from .copybook_connector import CopybookConnector
        return CopybookConnector(**kwargs)
    if st in ("mcp", "mcp-postgres", "aurora-mcp", "aurora_mcp", "data_api"):
        from .mcp_connector import MCPPostgresConnector
        return MCPPostgresConnector(**kwargs)
    if st in ("database", "db"):
        if "url" in kwargs:
            return DBConnector(kwargs["url"], schema=kwargs.get("schema"))
        return DBConnector.from_params(**kwargs)
    # treat as a dialect shortcut
    return DBConnector.from_params(dialect=st, **kwargs)


__all__ = ["Connector", "FileConnector", "DBConnector", "build_url",
           "get_connector"]
