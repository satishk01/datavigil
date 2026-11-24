"""Core data models shared across all DataVigil engines.

These are plain dataclasses (no heavy ORM) so results are trivially
JSON-serialisable for the API, the dashboard, and stored run history.
"""
from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import math
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def to_jsonable(obj: Any) -> Any:
    """Recursively convert dataclasses / enums / datetimes to JSON-safe types."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {k: to_jsonable(v) for k, v in dataclasses.asdict(obj).items()}
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (list, tuple)):
        return [to_jsonable(v) for v in obj]
    if isinstance(obj, dict):
        return {k: to_jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    if isinstance(obj, float):
        # NaN / inf are not JSON-compliant; the API's encoder rejects them
        return obj if math.isfinite(obj) else None
    return obj


# --------------------------------------------------------------------------
# Data quality dimensions (industry-standard set)
# --------------------------------------------------------------------------
class DQDimension(str, enum.Enum):
    COMPLETENESS = "completeness"    # not-null / not-blank
    UNIQUENESS = "uniqueness"        # no unexpected duplicates
    VALIDITY = "validity"            # conforms to format / range / set
    CONSISTENCY = "consistency"      # cross-column / cross-table agreement
    ACCURACY = "accuracy"            # matches a reference / expected value
    TIMELINESS = "timeliness"        # freshness / recency
    INTEGRITY = "integrity"          # referential integrity


class Severity(str, enum.Enum):
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"


# --------------------------------------------------------------------------
# Profiling result models
# --------------------------------------------------------------------------
@dataclass
class ColumnProfile:
    name: str
    inferred_type: str                       # numeric | string | datetime | boolean | categorical
    dtype: str                               # raw pandas dtype
    count: int = 0
    null_count: int = 0
    null_pct: float = 0.0
    distinct_count: int = 0
    distinct_pct: float = 0.0
    is_unique: bool = False
    # numeric
    min: Optional[float] = None
    max: Optional[float] = None
    mean: Optional[float] = None
    median: Optional[float] = None
    std: Optional[float] = None
    q25: Optional[float] = None
    q75: Optional[float] = None
    zeros: Optional[int] = None
    negatives: Optional[int] = None
    # string
    min_length: Optional[int] = None
    max_length: Optional[int] = None
    mean_length: Optional[float] = None
    blank_count: Optional[int] = None
    # datetime
    min_date: Optional[str] = None
    max_date: Optional[str] = None
    # shared
    top_values: list[dict] = field(default_factory=list)   # [{value, count, pct}]
    patterns: list[dict] = field(default_factory=list)      # [{pattern, count, pct, example}]
    sample_values: list[Any] = field(default_factory=list)


@dataclass
class CorrelationPair:
    col_a: str
    col_b: str
    coefficient: float
    method: str = "pearson"


@dataclass
class TableProfile:
    dataset: str
    row_count: int
    column_count: int
    duplicate_row_count: int = 0
    profiled_rows: Optional[int] = None     # set when profiling ran on a sample
    columns: list[ColumnProfile] = field(default_factory=list)
    correlations: list[CorrelationPair] = field(default_factory=list)
    generated_at: str = field(default_factory=_now)
    llm_summary: Optional[str] = None
    id: str = field(default_factory=_new_id)


# --------------------------------------------------------------------------
# Rule models
# --------------------------------------------------------------------------
@dataclass
class Rule:
    name: str
    rule_type: str                 # registry key, e.g. "not_null", "range", "regex"
    column: Optional[str] = None
    params: dict = field(default_factory=dict)
    dimension: DQDimension = DQDimension.VALIDITY
    severity: Severity = Severity.ERROR
    threshold: float = 1.0         # fraction of rows that must pass (1.0 = all)
    description: str = ""
    enabled: bool = True
    weight: float = 1.0            # contribution to the DQ score
    id: str = field(default_factory=_new_id)
    source: str = "manual"         # manual | nl | suggested

    def __post_init__(self):
        if isinstance(self.dimension, str):
            self.dimension = DQDimension(self.dimension)
        if isinstance(self.severity, str):
            self.severity = Severity(self.severity)


@dataclass
class RuleResult:
    rule_id: str
    rule_name: str
    rule_type: str
    column: Optional[str]
    dimension: DQDimension
    severity: Severity
    passed: bool
    rows_evaluated: int
    rows_failed: int
    pass_rate: float
    threshold: float
    weight: float
    message: str = ""
    failed_examples: list[Any] = field(default_factory=list)
    error: Optional[str] = None


@dataclass
class RunResult:
    dataset: str
    results: list[RuleResult] = field(default_factory=list)
    score: float = 0.0                       # 0-100 weighted DQ score
    dimension_scores: dict = field(default_factory=dict)
    total_rules: int = 0
    passed_rules: int = 0
    failed_rules: int = 0
    row_count: int = 0
    started_at: str = field(default_factory=_now)
    finished_at: Optional[str] = None
    id: str = field(default_factory=_new_id)


# --------------------------------------------------------------------------
# Source -> target mapping / reconciliation models
# --------------------------------------------------------------------------
class Cardinality(str, enum.Enum):
    ONE_ONE = "1:1"       # each source row -> exactly one target row
    ONE_MANY = "1:N"      # each source row -> many target rows (fan-out)
    MANY_ONE = "N:1"      # many source rows -> one target row (aggregation)
    MANY_MANY = "M:N"     # join-table style relationship


@dataclass
class ColumnMap:
    """One source -> target column pair.

    `transform` is a pandas-df.eval expression over the SOURCE frame used for
    derived columns, e.g. "first_name + ' ' + last_name" or "qty * unit_price".
    When set, `src` may be omitted — the result lands in `tgt`.
    """
    tgt: str
    src: Optional[str] = None
    transform: Optional[str] = None
    tolerance: float = 0.0           # allowed |src - tgt| for numeric compares


@dataclass
class DatasetMapping:
    """How a source dataset maps to a target (staging/target) dataset."""
    name: str
    source_a: str                    # source connection name
    dataset_a: str
    source_b: str                    # target connection name
    dataset_b: str
    cardinality: Cardinality = Cardinality.ONE_ONE
    key_pairs: list = field(default_factory=list)  # [{"src": "id", "tgt": "order_id"}]
    columns: list = field(default_factory=list)    # list[ColumnMap]
    aggregates: list = field(default_factory=list) # [{"column": "amt", "op": "sum", "tolerance": 0.01}]
    enabled: bool = True
    id: str = field(default_factory=_new_id)

    def __post_init__(self):
        if isinstance(self.cardinality, str):
            self.cardinality = Cardinality(self.cardinality)
        # "auto" is a marker expanded by the service once it can see the data
        if self.columns != "auto":
            self.columns = [c if isinstance(c, ColumnMap) else ColumnMap(**c)
                            for c in (self.columns or [])]


# --------------------------------------------------------------------------
# Anomaly models
# --------------------------------------------------------------------------
@dataclass
class Anomaly:
    column: Optional[str]
    method: str
    row_index: Optional[int]
    value: Any
    score: float
    reason: str
    severity: Severity = Severity.WARNING


@dataclass
class AnomalyResult:
    dataset: str
    anomalies: list[Anomaly] = field(default_factory=list)
    total_rows: int = 0
    anomaly_count: int = 0
    methods_used: list[str] = field(default_factory=list)
    generated_at: str = field(default_factory=_now)
    id: str = field(default_factory=_new_id)
