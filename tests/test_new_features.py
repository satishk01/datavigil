"""Tests for Phase 1-4 features: file options, copybook, reconcile,
schema contracts, batch.

Run with:  pytest tests/ -q     (or python tests/test_new_features.py)
"""
import os
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytest

os.environ.setdefault("DATAVIGIL_HOME", tempfile.mkdtemp())
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from datavigil.connectors import get_connector                       # noqa: E402
from datavigil.connectors.copybook_connector import (               # noqa: E402
    CopybookConnector, parse_copybook, flatten)
from datavigil.core.models import DatasetMapping                     # noqa: E402
from datavigil.core.service import DataVigilService                  # noqa: E402


@pytest.fixture()
def tmp(tmp_path):
    return tmp_path


# ---------------------------------------------------------------- files ---
def test_pipe_delimited(tmp):
    f = tmp / "p.txt"
    f.write_text("id|name|amt\n1|a|1.5\n2|b|2.5\n")
    c = get_connector("file", path=str(f))
    df = c.load("x")
    assert list(df.columns) == ["id", "name", "amt"] and len(df) == 2


def test_semicolon_decimal(tmp):
    f = tmp / "s.csv"
    f.write_text("id;amt\n1;10,5\n")
    df = get_connector("file", path=str(f), sep=";", decimal=",").load("x")
    assert df["amt"].iloc[0] == 10.5


def test_ndjson(tmp):
    f = tmp / "r.ndjson"
    f.write_text('{"id":1}\n{"id":2}\n{"id":3}\n')
    c = get_connector("file", path=str(f))
    assert len(c.load("x")) == 3 and c.row_count("x") == 3


def test_fixed_width(tmp):
    f = tmp / "f.fwf"
    f.write_text("ID001ALPHA  0100\nID002BETA   0250\n")
    df = get_connector("file", path=str(f),
                       widths=[5, 7, 4], names=["id", "nm", "amt"]).load("x")
    assert df["amt"].tolist() == [100, 250]


def test_json_array_still_works(tmp):
    f = tmp / "j.json"
    f.write_text('[{"id": 1}, {"id": 2}]')
    assert len(get_connector("file", path=str(f)).load("x")) == 2


# ------------------------------------------------------------- copybook ---
COPYBOOK = """       01  REC.
           05 CUST-ID      PIC X(5).
           05 AMT          PIC S9(5)V99 COMP-3.
           05 CNT          PIC 9(4) COMP.
           05 FILLER       PIC X(2).
"""


def _packed(digits, neg=False):
    nib = list(digits) + ["D" if neg else "C"]
    if len(nib) % 2:
        nib = ["0"] + nib
    out = bytearray()
    for i in range(0, len(nib), 2):
        out.append(int(nib[i], 16) << 4 | int(nib[i + 1], 16))
    return bytes(out)


def test_copybook_layout():
    roots = parse_copybook(COPYBOOK)
    fields, rec_len = flatten(roots[0])
    names = [f.name for f in fields]
    assert "FILLER" not in names
    assert rec_len == 5 + 4 + 2 + 2  # x(5) + packed(7 digits->4) + comp(2) + filler(2)
    amt = next(f for f in fields if f.name == "AMT")
    assert (amt.offset, amt.length, amt.category) == (5, 4, "packed")


def test_copybook_decode(tmp):
    cpy = tmp / "t.cpy"
    cpy.write_text(COPYBOOK)
    rec = ("ABC  ".encode("cp037") + _packed("0012345") +
           (9).to_bytes(2, "big") + "  ".encode("cp037"))
    dat = tmp / "t.dat"
    dat.write_bytes(rec)
    df = CopybookConnector(str(dat), copybook=str(cpy)).load("REC")
    assert df["CUST-ID"].iloc[0] == "ABC"
    assert df["AMT"].iloc[0] == pytest.approx(123.45)
    assert df["CNT"].iloc[0] == 9


# ------------------------------------------------------------ reconcile ---
def _svc(tmp, monkeypatch):
    monkeypatch.setenv("DATAVIGIL_HOME", str(tmp / "home"))
    return DataVigilService()


def test_reconcile_11(tmp, monkeypatch):
    svc = _svc(tmp, monkeypatch)
    pd.DataFrame({"id": [1, 2, 3], "v": [10, 20, 30]}).to_csv(tmp / "a.csv", index=False)
    pd.DataFrame({"id": [1, 2, 4], "v": [10, 99, 30]}).to_csv(tmp / "b.csv", index=False)
    svc.register_source("a", "file", path=str(tmp / "a.csv"))
    svc.register_source("b", "file", path=str(tmp / "b.csv"))
    res = svc.reconcile({"name": "m", "source_a": "a", "dataset_a": "a.csv",
                         "source_b": "b", "dataset_b": "b.csv",
                         "cardinality": "1:1",
                         "key_pairs": [{"src": "id", "tgt": "id"}],
                         "columns": [{"src": "v", "tgt": "v"}]})
    assert not res["passed"]
    byname = {c["name"]: c for c in res["checks"]}
    assert not byname["missing_in_target"]["passed"]
    assert not byname["orphans_in_target"]["passed"]
    assert not byname["value_match:v"]["passed"]


def test_reconcile_1n_and_derived(tmp, monkeypatch):
    svc = _svc(tmp, monkeypatch)
    pd.DataFrame({"oid": [1, 2], "f": ["a", "b"], "l": ["x", "y"],
                  "q": [2, 1], "p": [10.0, 5.0]}).to_csv(tmp / "o.csv", index=False)
    pd.DataFrame({"oid": [1, 1, 2], "nm": ["a x", "a x", "b y"],
                  "tot": [20.0, 20.0, 5.0]}).to_csv(tmp / "l.csv", index=False)
    svc.register_source("o", "file", path=str(tmp / "o.csv"))
    svc.register_source("l", "file", path=str(tmp / "l.csv"))
    res = svc.reconcile({"name": "m", "source_a": "o", "dataset_a": "o.csv",
                         "source_b": "l", "dataset_b": "l.csv",
                         "cardinality": "1:N",
                         "key_pairs": [{"src": "oid", "tgt": "oid"}],
                         "columns": [
                             {"tgt": "nm", "transform": "f + ' ' + l"},
                             {"tgt": "tot", "transform": "q * p",
                              "tolerance": 0.001}]})
    assert res["passed"], [c for c in res["checks"] if not c["passed"]]


def test_reconcile_aggregates(tmp, monkeypatch):
    svc = _svc(tmp, monkeypatch)
    pd.DataFrame({"amt": [10, 20, 30]}).to_csv(tmp / "a.csv", index=False)
    pd.DataFrame({"amt": [10, 20]}).to_csv(tmp / "b.csv", index=False)
    svc.register_source("a", "file", path=str(tmp / "a.csv"))
    svc.register_source("b", "file", path=str(tmp / "b.csv"))
    res = svc.reconcile({"name": "m", "source_a": "a", "dataset_a": "a.csv",
                         "source_b": "b", "dataset_b": "b.csv",
                         "cardinality": "1:1",
                         "aggregates": [{"column": "amt", "op": "sum",
                                         "tolerance": 0.0}]})
    agg = next(c for c in res["checks"] if c["name"].startswith("aggregate"))
    assert not agg["passed"] and not res["passed"]


# ------------------------------------------------------------ schema gate --
def test_schema_gate_breaking(tmp, monkeypatch):
    svc = _svc(tmp, monkeypatch)
    pd.DataFrame({"cid": [1, 2], "v": ["a", "b"]}).to_csv(tmp / "f.csv", index=False)
    svc.register_source("s", "file", path=str(tmp / "f.csv"))
    svc.pin_schema("s", "f.csv")
    assert svc.schema_gate("s", "f.csv")["passed"]
    # break it: cid becomes string
    pd.DataFrame({"cid": ["X1", "X2"], "v": ["a", "b"]}).to_csv(
        tmp / "f.csv", index=False)
    res = svc.schema_gate("s", "f.csv")
    assert not res["passed"]
    assert res["breaking"][0]["column"] == "cid"


def test_schema_gate_added_column_ok(tmp, monkeypatch):
    svc = _svc(tmp, monkeypatch)
    pd.DataFrame({"cid": [1]}).to_csv(tmp / "f.csv", index=False)
    svc.register_source("s", "file", path=str(tmp / "f.csv"))
    svc.pin_schema("s", "f.csv")
    pd.DataFrame({"cid": [1], "extra": ["z"]}).to_csv(tmp / "f.csv", index=False)
    res = svc.schema_gate("s", "f.csv")
    assert res["passed"] and res["compatible"]


# ---------------------------------------------------------------- batch ---
def test_batch(tmp, monkeypatch):
    svc = _svc(tmp, monkeypatch)
    pd.DataFrame({"id": [1, 2], "v": [1.0, 2.0]}).to_csv(tmp / "a.csv", index=False)
    manifest = {"jobs": [
        {"name": "g", "type": "gate", "source_file": str(tmp / "a.csv"),
         "pin_if_missing": True},
        {"name": "p", "type": "pair", "source_file": str(tmp / "a.csv"),
         "target_file": str(tmp / "a.csv"),
         "mapping": {"cardinality": "1:1", "columns": "auto"}},
    ]}
    rep = svc.run_batch(manifest)
    assert rep["passed"] and rep["jobs_total"] == 2


if __name__ == "__main__":
    # run without pytest installed
    import types
    tmpcount = [0]

    class _Monkey:
        def setenv(self, k, v):
            os.environ[k] = v
    monkey = _Monkey()

    fns = [v for k, v in list(globals().items())
           if k.startswith("test_") and callable(v)]
    failed = 0
    for fn in fns:
        td = Path(tempfile.mkdtemp())
        try:
            if "monkeypatch" in fn.__code__.co_varnames:
                fn(td, monkey)
            elif "tmp" in fn.__code__.co_varnames:
                fn(td)
            else:
                fn()
            print(f"PASS {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL {fn.__name__}: {e}")
    print(f"{len(fns) - failed}/{len(fns)} passed")
    sys.exit(1 if failed else 0)
