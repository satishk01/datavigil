"""IMS/COBOL copybook connector.

Reads fixed-length mainframe records described by a COBOL/IMS copybook into a
DataFrame, so copybook-defined files get the same profiling / rules / anomaly /
reconciliation treatment as every other source.

Supported copybook features:
  - level numbers (01-49), group items, 88-level condition names (skipped)
  - PIC X(n)               alphanumeric
  - PIC 9 / S9, with V     zoned (display) decimal, incl. overpunch sign
  - PIC 9 / S9 COMP-3      packed decimal
  - PIC 9 / S9 COMP/COMP-4/COMP-5/BINARY  binary integer
  - OCCURS n TIMES (and OCCURS x TO y DEPENDING — uses the maximum)
  - REDEFINES              parsed at the same offset; skipped from output by
                           default (option include_redefines=True keeps them)
  - FILLER                 occupies space, not emitted as a column
  - multiple 01 levels     each becomes a dataset (alternate record layouts)

Options (connector params / `options` dict):
  copybook            path to the copybook file (required)
  encoding            'cp037' (EBCDIC, default) | 'ascii'/'latin-1' for ASCII
                      data files
  copybook_encoding   encoding of the copybook text itself (default utf-8)
  include_redefines   also emit REDEFINES interpretations (default False)
  record_delimiter    'none' (fixed-size records, default) | 'newline'
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterator, Optional

import pandas as pd

from .base import Connector


# --------------------------------------------------------------------------
# Copybook parsing
# --------------------------------------------------------------------------
@dataclass
class _Node:
    level: int
    name: str
    pic: str = ""
    usage: str = "display"          # display | packed | binary
    occurs: int = 1
    redefines: Optional[str] = None
    children: list = field(default_factory=list)
    # computed
    signed: bool = False
    int_digits: int = 0
    frac_digits: int = 0
    alpha_len: int = 0
    size: int = 0                   # storage bytes for one occurrence
    offset: int = 0


@dataclass
class Field:
    """A flattened, decodable leaf field."""
    name: str
    offset: int
    length: int
    category: str                    # alpha | zoned | packed | binary
    int_digits: int = 0
    frac_digits: int = 0
    signed: bool = False


_LINE = re.compile(
    r"^\s*(\d{2})\s+([A-Za-z0-9][\w-]*|FILLER)\b(.*)$", re.IGNORECASE)
_PIC = re.compile(r"PIC(?:TURE)?\s+(?:IS\s+)?([^\s.]+)", re.IGNORECASE)
_OCCURS = re.compile(
    r"OCCURS\s+(\d+)(?:\s+TO\s+(\d+))?(?:\s+TIMES)?(?:\s+DEPENDING(?:\s+ON\s+\S+)?)?",
    re.IGNORECASE)
_REDEF = re.compile(r"REDEFINES\s+([\w-]+)", re.IGNORECASE)


def _expand_pic(pic: str) -> str:
    """9(7)V99 -> 9999999V99 ; X(10) -> XXXXXXXXXX"""
    out = []
    i = 0
    while i < len(pic):
        ch = pic[i].upper()
        if ch in ("X", "9", "A") and i + 1 < len(pic) and pic[i + 1] == "(":
            j = pic.index(")", i)
            out.append(ch * int(pic[i + 2:j]))
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _pic_info(pic: str) -> tuple[str, int, int, bool]:
    """Return (kind, int_digits, frac_digits, signed).
    kind: 'alpha' | 'numeric'."""
    if not pic:
        return "group", 0, 0, False
    exp = _expand_pic(pic)
    signed = exp.startswith("S")
    if signed:
        exp = exp[1:]
    if exp.startswith(("X", "A")):
        return "alpha", len(exp), 0, signed
    if "V" in exp:
        int_part, frac_part = exp.split("V", 1)
        return "numeric", len(int_part), len(frac_part), signed
    return "numeric", len(exp), 0, signed


def _usage_of(clause: str) -> str:
    c = clause.upper()
    if "COMP-3" in c or "PACKED-DECIMAL" in c:
        return "packed"
    if re.search(r"\b(COMP|COMP-4|COMP-5|BINARY)\b", c):
        return "binary"
    return "display"


def _binary_size(digits: int) -> int:
    if digits <= 4:
        return 2
    if digits <= 9:
        return 4
    return 8


def _leaf_size(node: _Node) -> int:
    if node.pic_kind == "alpha":
        return node.alpha_len
    d = node.int_digits + node.frac_digits
    if node.usage == "packed":
        return d // 2 + 1
    if node.usage == "binary":
        return _binary_size(d)
    return max(d, 1)


def parse_copybook(text: str) -> list[_Node]:
    """Parse copybook text into a list of 01-level trees."""
    roots: list[_Node] = []
    stack: list[_Node] = []

    # build logical statements — a clause may continue onto following lines
    # until the terminating period
    stmts: list[str] = []
    buf = ""
    for raw in text.splitlines():
        # strip sequence numbers (cols 1-6), indicators (col 7), comments
        if len(raw) > 6 and raw[:6].strip().isdigit():
            raw = raw[6:]
        s = raw.strip()
        if not s or s.startswith("*"):
            continue
        buf += " " + s
        if s.endswith("."):
            stmts.append(buf)
            buf = ""
    if buf.strip():
        stmts.append(buf)

    for stmt in stmts:
        m = _LINE.match(stmt)
        if not m:
            continue
        level, name, clause = int(m.group(1)), m.group(2).upper(), m.group(3)
        if level in (66, 77, 88):
            continue
        node = _Node(level=level, name=name)
        pm = _PIC.search(clause)
        if pm:
            node.pic = pm.group(1)
        node.pic_kind, node.int_digits, node.frac_digits, node.signed = \
            _pic_info(node.pic)
        if node.pic_kind == "alpha":
            node.alpha_len = node.int_digits
            node.int_digits = 0
        node.usage = _usage_of(clause)
        om = _OCCURS.search(clause)
        if om:
            node.occurs = int(om.group(2) or om.group(1))
        rm = _REDEF.search(clause)
        if rm:
            node.redefines = rm.group(1).upper()

        while stack and stack[-1].level >= level:
            stack.pop()
        if stack:
            stack[-1].children.append(node)
        else:
            roots.append(node)
        stack.append(node)

    if not roots:
        raise ValueError("No 01-level record found in copybook")

    # size = storage of ONE occurrence (children's own OCCURS are inside it);
    # total footprint of a node is n.size * n.occurs
    def size(n: _Node) -> int:
        if n.children:
            n.size = sum(size(c) * c.occurs for c in n.children
                         if not c.redefines)
        else:
            n.size = _leaf_size(n)
        return n.size

    def layout(n: _Node):
        prev_end = n.offset
        prev: Optional[_Node] = None
        for c in n.children:
            c.offset = prev_end if not c.redefines else \
                (prev.offset if prev else prev_end)
            layout(c)
            if not c.redefines:
                prev_end = c.offset + c.size * c.occurs
                prev = c
        # group storage = span of children
        n.size = max(n.size, prev_end - n.offset)

    for r in roots:
        size(r)
        r.offset = 0
        if r.children:
            layout(r)
    return roots


def flatten(root: _Node, include_redefines: bool = False) -> tuple[list[Field], int]:
    """Flatten a 01 tree into (fields, record_length).

    Offsets were computed relative to the containing 01 during `layout`, but
    children carry the offset of their position inside the parent chain, so we
    walk the tree accumulating (parent_base + own_offset). OCCURS expands into
    one column per element, suffixed _1.._n.
    """
    fields: list[Field] = []

    def to_field(n: _Node, off: int, name: str) -> Field:
        cat = "alpha" if n.pic_kind == "alpha" else {
            "display": "zoned", "packed": "packed", "binary": "binary",
        }[n.usage]
        return Field(name=name, offset=off, length=n.size, category=cat,
                     int_digits=n.int_digits, frac_digits=n.frac_digits,
                     signed=n.signed)

    def walk(n: _Node, base: int, suffix: str):
        if n.redefines and not include_redefines:
            return
        for i in range(n.occurs):
            off = base + i * n.size
            sfx = f"{suffix}_{i + 1}" if n.occurs > 1 else suffix
            if n.children:
                for c in n.children:
                    walk(c, off + (c.offset - n.offset), sfx)
            else:
                if n.name == "FILLER":
                    continue            # occupies space, not a column
                nm = f"{n.name}{sfx}" if sfx else n.name
                fields.append(to_field(n, off, nm))

    walk(root, 0, "")
    # record length is the full 01 storage — trailing FILLER still consumes bytes
    rec_len = max(root.size,
                  max((f.offset + f.length for f in fields), default=0))
    return fields, rec_len


# --------------------------------------------------------------------------
# Record decoding
# --------------------------------------------------------------------------
def _decode_zoned(raw: bytes, signed: bool, scale: int, encoding: str):
    if not raw:
        return None
    neg = False
    digits = []
    last = raw[-1]
    if encoding.lower() in ("ascii", "latin-1", "utf-8"):
        # ASCII zoned: digit bytes 0x30-0x39; sign often via overpunch chars
        # { } A-I J-R or a leading '-'/'+'.
        body = raw
        if raw[:1] in (b"-", b"+"):
            neg = raw[:1] == b"-"
            body = raw[1:]
            last = body[-1] if body else 0
        if signed:
            ch = chr(last)
            if ch == "}":
                neg, last = True, ord("0")
            elif ch in "JKLMNOPQR":
                neg, last = True, ord("1") + "JKLMNOPQR".index(ch)
            elif ch == "{":
                last = ord("0")
            elif ch in "ABCDEFGHI":
                last = ord("1") + "ABCDEFGHI".index(ch)
        for b in body[:-1]:
            digits.append(b & 0x0F)
        digits.append(last & 0x0F if isinstance(last, int) else last[0] & 0x0F)
    else:  # EBCDIC
        zone = (last >> 4) & 0x0F
        if signed:
            neg = zone == 0x0D
        for b in raw[:-1]:
            digits.append(b & 0x0F)
        digits.append(last & 0x0F)
    val = int("".join(str(d) for d in digits) or "0")
    if neg:
        val = -val
    return val / (10 ** scale) if scale else val


def _decode_packed(raw: bytes, scale: int):
    if not raw:
        return None
    sign_nib = raw[-1] & 0x0F
    neg = sign_nib in (0x0D, 0x0B)
    nibbles = []
    for b in raw:
        nibbles.append((b >> 4) & 0x0F)
        nibbles.append(b & 0x0F)
    nibbles = nibbles[:-1]          # drop sign nibble
    val = int("".join(str(n) for n in nibbles) or "0")
    if neg:
        val = -val
    return val / (10 ** scale) if scale else val


def decode_record(rec: bytes, fields: list[Field], encoding: str) -> dict:
    row = {}
    for f in fields:
        raw = rec[f.offset:f.offset + f.length]
        try:
            if f.category == "alpha":
                row[f.name] = raw.decode(encoding, errors="replace").rstrip()
            elif f.category == "zoned":
                row[f.name] = _decode_zoned(raw, f.signed, f.frac_digits, encoding)
            elif f.category == "packed":
                row[f.name] = _decode_packed(raw, f.frac_digits)
            else:  # binary
                row[f.name] = int.from_bytes(raw, "big", signed=f.signed) \
                    / (10 ** f.frac_digits) if f.frac_digits else \
                    int.from_bytes(raw, "big", signed=f.signed)
        except Exception:
            row[f.name] = None
    return row


# --------------------------------------------------------------------------
# Connector
# --------------------------------------------------------------------------
class CopybookConnector(Connector):
    kind = "copybook"

    def __init__(self, path: str, copybook: str, encoding: str = "cp037",
                 copybook_encoding: str = "utf-8",
                 include_redefines: bool = False,
                 record_delimiter: str = "none", **_ignored):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"Path does not exist: {path}")
        cb_path = Path(copybook)
        if not cb_path.exists():
            raise FileNotFoundError(f"Copybook does not exist: {copybook}")
        self.encoding = encoding
        self.delimiter = record_delimiter.lower()
        cb_text = cb_path.read_text(encoding=copybook_encoding, errors="replace")
        self._roots = {r.name: r for r in parse_copybook(cb_text)}
        self._layouts = {
            name: flatten(r, include_redefines) for name, r in self._roots.items()
        }

    def list_datasets(self) -> list[str]:
        return list(self._roots)

    def _records(self, rec_len: int) -> Iterator[bytes]:
        data = self.path.read_bytes()
        if self.delimiter == "newline":
            for line in data.splitlines():
                if len(line) >= rec_len:
                    yield line[:rec_len]
            return
        if rec_len <= 0:
            return
        for i in range(0, len(data) - rec_len + 1, rec_len):
            yield data[i:i + rec_len]

    def load(self, dataset: str, limit: Optional[int] = None) -> pd.DataFrame:
        if dataset not in self._layouts:
            # tolerate case variants
            match = next((k for k in self._layouts
                          if k.upper() == dataset.upper()), None)
            if match is None:
                raise KeyError(f"Unknown record layout '{dataset}' — "
                               f"available: {sorted(self._layouts)}")
            dataset = match
        fields, rec_len = self._layouts[dataset]
        rows = []
        for i, rec in enumerate(self._records(rec_len)):
            if limit and i >= limit:
                break
            rows.append(decode_record(rec, fields, self.encoding))
        return pd.DataFrame(rows, columns=[f.name for f in fields])

    def row_count(self, dataset: str) -> Optional[int]:
        ds = dataset if dataset in self._layouts else \
            next((k for k in self._layouts if k.upper() == dataset.upper()), dataset)
        if ds not in self._layouts:
            return None
        _, rec_len = self._layouts[ds]
        if rec_len <= 0:
            return None
        if self.delimiter == "newline":
            with open(self.path, "rb") as fh:
                return sum(1 for _ in fh)
        return self.path.stat().st_size // rec_len
