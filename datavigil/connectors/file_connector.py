"""File connectors: CSV, delimited text, Excel, Parquet, JSON/NDJSON, fixed-width.

A single file is treated as one dataset. For Excel, each sheet is a dataset.
A directory is treated as a collection of datasets (one per supported file).

Reader options (passed to FileConnector via `options` or as kwargs):
  sep / delimiter   column separator for delimited files (default ',' for
                    .csv, '\\t' for .tsv, auto-sniffed for .txt/.dat)
  encoding          text encoding (default utf-8; e.g. 'latin-1', 'cp037')
  header            True (default) | False | int row number of the header
  skiprows          rows to skip at the top of the file
  json_lines        force line-delimited JSON (NDJSON); automatic for
                    .jsonl/.ndjson and retried automatically for .json
  fixed_width       parse with pandas.read_fwf (default for .fwf)
  widths            list of fixed column widths for read_fwf
  colspecs          list of (start, end) tuples for read_fwf
  names             column names (useful with header=False / fixed-width)
"""
from __future__ import annotations

import csv
from pathlib import Path
from typing import Optional

import pandas as pd

from .base import Connector

_CSV = {".csv"}
_TSV = {".tsv", ".tab"}
_DELIMITED = {".txt", ".dat", ".csv", ".tsv", ".tab"}   # delimited text family
_EXCEL = {".xlsx", ".xls", ".xlsm"}
_PARQUET = {".parquet", ".pq"}
_JSON = {".json"}
_NDJSON = {".jsonl", ".ndjson"}
_FWF = {".fwf"}
_SUPPORTED = _CSV | _TSV | {".txt", ".dat"} | _EXCEL | _PARQUET | _JSON | _NDJSON | _FWF

# reader options we understand (rest are ignored rather than exploding)
_OPT_KEYS = {"sep", "delimiter", "encoding", "header", "skiprows",
             "json_lines", "fixed_width", "widths", "colspecs", "names",
             "na_values", "thousands", "decimal", "quotechar", "comment",
             "sheet", "dtype"}


class FileConnector(Connector):
    kind = "file"

    def __init__(self, path: str, options: Optional[dict] = None, **opts):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")
        merged = dict(options or {})
        merged.update(opts)
        self.options = {k: v for k, v in merged.items()
                        if k in _OPT_KEYS and v is not None}

    # -- discovery ---------------------------------------------------------
    def _files(self) -> list[Path]:
        if self.path.is_dir():
            return sorted(
                p for p in self.path.iterdir()
                if p.is_file() and p.suffix.lower() in _SUPPORTED
            )
        return [self.path]

    def list_datasets(self) -> list[str]:
        out: list[str] = []
        for f in self._files():
            if f.suffix.lower() in _EXCEL:
                try:
                    xl = pd.ExcelFile(f)
                    out.extend(f"{f.name}::{sheet}" for sheet in xl.sheet_names)
                except Exception:
                    out.append(f.name)
            else:
                out.append(f.name if self.path.is_dir() else str(f))
        return out

    # -- loading -----------------------------------------------------------
    def _resolve(self, dataset: str) -> tuple[Path, Optional[str]]:
        sheet = None
        name = dataset
        if "::" in dataset:
            name, sheet = dataset.split("::", 1)
        if self.path.is_dir():
            target = self.path / name
        else:
            target = self.path
        return target, sheet

    def _delim_kwargs(self, suffix: str) -> dict:
        """pandas.read_csv kwargs for the delimited-text family."""
        o = self.options
        kw: dict = {"low_memory": False}
        sep = o.get("sep", o.get("delimiter"))
        if sep is not None:
            kw["sep"] = sep
        elif suffix in _TSV:
            kw["sep"] = "\t"
        elif suffix in {".txt", ".dat"}:
            # no explicit delimiter for generic text files — let the engine sniff
            kw.update(sep=None, engine="python")
        if o.get("encoding"):
            kw["encoding"] = o["encoding"]
        if "header" in o:
            h = o["header"]
            if h in (False, "false", "none"):
                kw["header"] = None
            elif h in (True, "true"):
                kw["header"] = 0
            else:
                kw["header"] = int(h)
        if o.get("skiprows"):
            kw["skiprows"] = o["skiprows"]
        if o.get("names"):
            kw["names"] = o["names"]
            kw.setdefault("header", None)
        for k in ("na_values", "thousands", "decimal", "quotechar", "comment"):
            if k in o:
                kw[k] = o[k]
        if o.get("dtype") in ("str", str):
            kw["dtype"] = str
        return kw

    def _sniff_delimiter(self, target: Path) -> Optional[str]:
        try:
            with open(target, "r", encoding=self.options.get("encoding", "utf-8"),
                      errors="replace") as fh:
                sample = fh.read(8192)
            dialect = csv.Sniffer().sniff(sample, delimiters=",\t;|:")
            return dialect.delimiter
        except Exception:
            return None

    def _fwf_kwargs(self) -> dict:
        o = self.options
        kw: dict = {}
        if o.get("widths"):
            kw["widths"] = o["widths"]
        if o.get("colspecs"):
            kw["colspecs"] = [tuple(c) for c in o["colspecs"]]
        if o.get("encoding"):
            kw["encoding"] = o["encoding"]
        if "header" in o:
            h = o["header"]
            if h in (False, "false", "none"):
                kw["header"] = None
            elif h in (True, "true"):
                kw["header"] = 0
            else:
                kw["header"] = int(h)
        else:
            kw["header"] = None
        if o.get("names"):
            kw["names"] = o["names"]
        if o.get("skiprows"):
            kw["skiprows"] = o["skiprows"]
        return kw

    def load(self, dataset: str, limit: Optional[int] = None) -> pd.DataFrame:
        target, sheet = self._resolve(dataset)
        suffix = target.suffix.lower()
        o = self.options

        if suffix in _EXCEL:
            df = pd.read_excel(target, sheet_name=o.get("sheet", sheet or 0),
                               nrows=limit)
        elif suffix in _PARQUET:
            df = pd.read_parquet(target)
            if limit:
                df = df.head(limit)
        elif suffix in _JSON | _NDJSON:
            lines = bool(o.get("json_lines")) or suffix in _NDJSON
            try:
                df = pd.read_json(target, lines=lines)
            except ValueError:
                # a .json file that is actually NDJSON (or vice-versa)
                df = pd.read_json(target, lines=not lines)
            if limit:
                df = df.head(limit)
        elif suffix in _FWF or o.get("fixed_width"):
            df = pd.read_fwf(target, nrows=limit, **self._fwf_kwargs())
        elif suffix in _DELIMITED:
            kw = self._delim_kwargs(suffix)
            if suffix in {".txt", ".dat"} and kw.get("sep") is None:
                sniffed = self._sniff_delimiter(target)
                if sniffed:
                    kw.pop("engine", None)
                    kw["sep"] = sniffed
            df = pd.read_csv(target, nrows=limit, **kw)
        else:
            raise ValueError(f"Unsupported file type: {suffix}")
        return df

    def row_count(self, dataset: str) -> Optional[int]:
        target, sheet = self._resolve(dataset)
        suffix = target.suffix.lower()
        try:
            if suffix in _NDJSON or (suffix in _JSON
                                     and self.options.get("json_lines")):
                with open(target, "rb") as fh:
                    return sum(1 for line in fh if line.strip())
            if suffix in _DELIMITED:
                header = self.options.get("header", True)
                offset = 0 if header in (False, "false", "none") else 1
                offset += int(self.options.get("skiprows") or 0)
                with open(target, "rb") as fh:
                    return max(sum(1 for _ in fh) - offset, 0)
            if suffix in _FWF:
                with open(target, "rb") as fh:
                    return max(sum(1 for _ in fh) - 1, 0)
            if suffix in _PARQUET:
                import pyarrow.parquet as pq  # type: ignore
                return pq.ParquetFile(target).metadata.num_rows
        except Exception:
            return None
        return None
