"""Connector abstraction.

A connector knows how to:
  - list available datasets (tables / sheets / files)
  - load a dataset into a pandas DataFrame (optionally sampled)
  - run a count query where it can be pushed down (DB connectors)

All profiling / rule / anomaly engines operate on the DataFrame returned
here, so adding a new source only means implementing this interface.
"""
from __future__ import annotations

import abc
from typing import Optional

import pandas as pd


class Connector(abc.ABC):
    kind: str = "base"

    @abc.abstractmethod
    def list_datasets(self) -> list[str]:
        """Return the names of datasets this connector exposes."""

    @abc.abstractmethod
    def load(self, dataset: str, limit: Optional[int] = None) -> pd.DataFrame:
        """Load a dataset (optionally the first `limit` rows) as a DataFrame."""

    def row_count(self, dataset: str) -> Optional[int]:
        """Best-effort total row count; None if unknown without a full load."""
        return None

    def close(self) -> None:  # pragma: no cover - optional
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()
