# DataVigil — Black-Box Test Guide

For a non-technical tester. No coding required — **every step runs in the
dashboard UI** (CLI equivalents are marked where they exist). Tick each ✅
when the expected result appears; if anything differs, record it under
*Reporting a problem* at the end.

> The same plan exists as a printable checklist in **`test-steps.pdf`**
> (regenerate with `python docs/build_test_steps_pdf.py`).

**What's covered:** every feature of the tool — file sources (CSV, custom
delimiters, JSON/NDJSON, fixed-width, IMS/COBOL copybooks), database sources
(Aurora direct, IAM auth, MCP Data API / pgwire), profiling, rules & scoring
(including different rules per table), anomaly detection, source→target
reconciliation (1:1 / 1:N / N:1 / M:N, derived columns, aggregates), schema
versioning & the breaking-change gate, batch manifests, audits & report export,
the REST API, the CLI — **and the same feature set run against a live Aurora
PostgreSQL schema** (section 9).

---

## 0. Start the app & sample data

**Windows (packaged build):** double-click `Start-DataVigil.bat`.
**From source:** `datavigil serve`, then open <http://127.0.0.1:8000>.

✅ **Expected:** Dashboard with six tabs — *Profiling, Rules & Score, Anomalies,
Reconcile, Audit & Report, Ask* — and a left panel offering four ways to load
data: **upload a file**, **register a local file / copybook**, **connect a
database**, **Aurora via MCP**.

Also check:

- ⚙ button (top-right) opens **LLM settings** — pick provider (AWS Bedrock /
  Ollama / OpenAI / Disabled) → Save. Everything except the AI cases works
  with `LLM: offline`.
- 🌙/☀ theme toggle (top-right corner) switches dark/light — cosmetic only.
- `GET http://127.0.0.1:8000/api/health` in a browser returns
  `{"status":"ok",...}`.

**Sample files** live in `samples/` — regenerate any time:

```
python samples/generate_samples.py
```

| File | Rows × Cols | Used for |
|------|-------------|----------|
| `parts_inventory.csv` | 2000 × 9 | profiling / rules / anomalies / audit |
| `customers_src.csv` + `stg_customers.csv` / `_bad` | 8 | 1:1 reconcile + derived `full_name` |
| `orders_src.csv` + `stg_orders.csv` / `_bad` | 12 | 1:1 reconcile + aggregate sum |
| `order_lines.csv` | 24 | 1:N fanout |
| `txn.csv` + `daily_summary.csv` / `_bad` | 20 / 4 | N:1 aggregate reconcile |
| `parts_pipe.txt`, `parts_semicolon.csv` | 30 | custom delimiters |
| `customers.ndjson` | 8 | JSON Lines |
| `customers_fwf.fwf` | 8 | fixed-width |
| `custcopy.cpy` + `custcopy.dat` | 3 | IMS/COBOL copybook (EBCDIC, COMP-3) |
| `customers_v2_breaking.csv`, `customers_v3_compatible.csv` | 8 | schema-gate scenarios |
| `enroll_src.csv` + `enroll_tgt.csv` / `enroll_tgt_bad.csv` | 5 | M:N junction reconcile (composite key) |
| `batch_manifest.yml` | 6 jobs | batch runner |
| `sql/aurora_setup.sql` | 14 tables | Aurora demo schema `demo` |

> Test 6.x also has you create `samples/gate_input.csv` yourself by copying
> another file over it — that simulates "a new extract landed with the same
> filename". Delete it when you are done (or leave it; it is ignored).

---

## 1. Data sources — files

### 1.1 Upload a CSV (file picker) ✅

1. Left panel → **Upload a file** → Choose File → `samples/parts_inventory.csv`.
2. Click `parts_inventory.csv` in the **Datasets** list.

✅ Toast *"Uploaded parts_inventory.csv → source \"parts_inventory\""*; the source dropdown switches to
the new source **and its dataset is auto-selected and activated** — no need
to pick anything manually. File datasets display by basename
(`parts_inventory.csv`) with the full path on hover; the same file can be
re-uploaded immediately (the picker resets).

### 1.2 Upload with reader options ✅

1. **First** paste `{"sep":";","decimal":","}` into the **reader options**
   box under the file picker — options must be filled in *before* you pick
   the file, because they are sent with the upload.
2. **Then** Choose File → `samples/parts_semicolon.csv`.
3. Profile it (Profiling tab → Profile dataset).

✅ **30 rows × 2 columns**; `unit_cost` is numeric. If you see 1 column of
garbage instead, you uploaded without the options — re-upload (the picker
resets, so choosing the same file again works).

### 1.3 Pipe-delimited `.txt` via path register ✅

1. **register a local file / copybook**: name `pipe_src`, path
   `samples/parts_pipe.txt`, options & copybook blank → **Register file source**.
2. Profile the dataset.

✅ **30 rows × 3 columns** — `|` auto-sniffed (or force `{"sep":"|"}`).

### 1.4 JSON Lines ✅

Register `nd_src`, path `samples/customers.ndjson`. Profile.

✅ **8 rows × 5 columns**; `customer_id` gets the **key** tag.

### 1.5 Fixed-width ✅

Register `fwf_src`, path `samples/customers_fwf.fwf`, options
`{"widths":[4,12,12,8],"names":["id","first","last","segment"]}`. Profile.

✅ **8 rows × 4 columns** named `id, first, last, segment`.

### 1.6 IMS/COBOL copybook (EBCDIC + COMP-3) ✅

Register `cb_src`: path `samples/custcopy.dat`, copybook `samples/custcopy.cpy`.

✅ Dataset **CUSTOMER-RECORD** (the 01-level); profile shows
**3 rows × 8 columns** — `CUST-ID, CUST-NAME, CUST-BALANCE, CUST-STATUS,
ORDER-COUNT, TAG-CD_1, TAG-CD_2, CUST-TYPE`. FILLER and the REDEFINES field
stay hidden. Packed-decimal balances decode to `12345.67, -9999.50, 500.00`;
binary `ORDER-COUNT` = `42, 7, 15`.

### 1.7 Register a folder ✅

Register `folder_src` with path `samples` (a directory, not a file).

✅ Every file in the folder becomes a dataset — this is what "audit whole
schema" iterates over in section 8.

### 1.8 Remove a source (no restart needed) ✅

1. Pick any source in the **source dropdown** → click **Remove source** →
   confirm.
2. Re-upload or re-register the same file — it works immediately (the file
   picker resets after each upload, so re-choosing the same filename fires
   the upload again).

✅ The source and its datasets disappear; its cached profiles/rules/runs are
cleared; you can immediately re-upload or re-register — no restart. For an
**uploaded** file the copy under `~/.datavigil/uploads/` is deleted too;
**path-registered** files are only unregistered (your file stays on disk).
API equivalent: `DELETE /api/sources/{name}` → `{"deleted": name}`
(404 if unknown).

---

## 2. Profiling

### 2.1 parts_inventory.csv ✅

Select the dataset → **Profiling** tab → **Profile dataset**.

✅ Cards: **Rows 2,000 · Columns 9 · Duplicate rows 0 · Correlated pairs 1**.
Columns table: `part_id` **key** (mask `A9{5}` 100%); `supplier` ~5.65% null
amber badge; `supplier_email` shows **email** pattern; `unit_cost` numeric
with **15 neg** in red; `last_restock_date` datetime. Correlations:
`unit_cost ↔ list_price r = 1.00`.

### 2.2 AI insights checkbox (optional, needs LLM) ✅

Tick **AI insights** → Profile again.

✅ A plain-language insight block appears above the columns table.

### 2.3 Compare two datasets — ad-hoc schema drift ✅

*What this proves:* the tool can diff any two datasets' schemas and column
stats on demand — no baseline, no mapping spec. Useful to eyeball "what
changed between my source extract and the staging table" before mapping.

**Step 1 — register the two datasets (once)**

1. Left panel → **register a local file / copybook**:
   - Name: `cust_src`, Path: `samples/customers_src.csv` →
     **Register file source**
2. ✅ Toast: *"Registered 'cust_src' (file) — 1 dataset(s)"*.
3. Repeat with Name: `v2`, Path: `samples/customers_v2_breaking.csv`.

**Step 2 — compare them (all in the dashboard, no API calls)**

1. Open the **Reconcile** tab.
2. Under **Source A (from)**: pick `cust_src` in the dropdown, then click
   `customers_src.csv` in the dataset list below it.
3. Under **Source B (to)**: pick `v2`, then `customers_v2_breaking.csv`.
4. Click **Compare schemas** (top-right, next to Suggest mapping).

✅ Five summary cards appear — **Rows A 8 · Rows B 8 · Row delta 0 ·
Only in A 1 · Only in B 0** — followed by a red pill `segment` under
*"Only in A — removed on B"*, and a column table where the
`customer_id` row shows `numeric → categorical` with a red **changed**
pill; the other three rows show green **same**.

✅ **PASS if** `segment` is the only red "Only in A" chip and exactly one
row (`customer_id`) is marked **changed**.

**Step 3 — negative check: comparing a file to itself**

Set Source B to `cust_src` / `customers_src.csv` as well →
**Compare schemas**.

✅ **0 Only in A, 0 Only in B**, no removed/added chips, and every row in
the column table is green **same** — a clean "no drift" result.

*Tip:* the same button compares a file against a database table or two
tables — just pick different sources for A and B (see DB-9).

---

## 3. Rules & Score

### 3.1 Suggest, run, score ✅

**Rules & Score** tab → **Suggest rules** → **Run & score**.

✅ ~18 rules appear (not-null on keys, ranges, set membership). Gauge shows a
0–100 score (**≈99.5** on the sample) with per-dimension bars; each rule row
shows pass/fail, pass-rate, failed count and example bad values
(e.g. `supplier mostly populated` passes at 94.3% with `null` examples).

### 3.2 Different rules per table ✅

1. Select `parts_inventory.csv` → Suggest rules (set A) → Run.
2. Select `customers_src.csv` → Suggest rules (set B — email format rule on
   `email`, etc.).
3. Switch datasets back and forth — the rules table **swaps automatically**
   the moment the dataset changes (no tab reopen needed), and the previous
   dataset's score/run results are cleared.

✅ Each dataset keeps **its own** rule set — A's rules never appear on B.
Rule sets persist across restarts in `~/.datavigil/state.json`.

### 3.3 Natural-language rule (AI) ✅

Type *"list_price must always be at least unit_cost"* → **Add via AI** → run.

✅ An **expression** rule appears (source = AI); on `parts_inventory.csv` it
fails ~0.4% of rows — the planted `list_price < unit_cost` violations.

### 3.4 Explain failures (AI) ✅

**Explain failures (AI)** → summary + issues table: each failing rule gets a
likely root cause and recommended fix, ordered by priority.

### 3.5 Rule run history ✅

1. With `parts_inventory.csv` selected, run **Run & score** once or twice
   in **Rules & Score**.
2. Switch to the **Audit & Report** tab — the **"Recent runs for this
   dataset"** table loads automatically at the bottom (no audit needed).

✅ Each run appears as a row — time (UTC), DQ score, rules passed/failed —
newest first, up to 15 rows. Selecting a different dataset swaps the table
to that dataset's history.

### 3.6 Rule set persistence via UI ✅

1. With `parts_inventory.csv` selected, open **Rules & Score** — the rules
   table lists the saved set (count shown in the header, e.g. *Rules (18)*).
2. Switch to `customers_src.csv` → a **different** ruleset shows.
3. Switch back → the original rules are still there.

✅ Each dataset keeps its own persistent ruleset — nothing leaks between
datasets. (The underlying endpoints — `GET /api/rules`, `/api/rule-types`,
`/api/history` — exist for integrations; the UI exercises the same data.)

---

## 4. Anomalies

### 4.1 parts_inventory.csv ✅

**Anomalies** tab → **Detect anomalies**.

✅ Cards: rows scanned 2,000, methods used. Table lists anomalous rows with
method (`iqr`, `isolation_forest`, `rare_category`, `pattern_break`), score,
column, value and reason — the planted extreme prices and malformed values
surface.

### 4.2 Real-time scorer (code-level, optional) ✅

The batch detector also ships a `RealtimeScorer` for scoring individual
incoming rows against a trained baseline — used from Python (no UI):

```
python -c "from datavigil.anomaly.detector import RealtimeScorer; print('ok')"
```

---

## 5. Reconcile — source → target mappings

Open the **Reconcile** tab. Source A = "from", Source B = "to". Register file
sources first: `cust_src` → `samples/customers_src.csv`, `stg` →
`samples/stg_customers.csv`, `stg_bad` → `samples/stg_customers_bad.csv`,
`ord_src` → `samples/orders_src.csv`, `ord_stg` → `samples/stg_orders.csv`,
`ord_stg_bad` → `samples/stg_orders_bad.csv`, `lines` →
`samples/order_lines.csv`, `tx` → `samples/txn.csv`, `ds` →
`samples/daily_summary.csv`, `ds_bad` → `samples/daily_summary_bad.csv`,
`enr_src` → `samples/enroll_src.csv`, `enr` → `samples/enroll_tgt.csv`,
`enr_bad` → `samples/enroll_tgt_bad.csv` (for the M:N case, 5.7).

### 5.1 Suggest mapping ✅

Pick A = cust_src/customers_src.csv, B = stg/stg_customers.csv →
**Suggest mapping**.

✅ Spec JSON prefilled: `customer_id` proposed as the key pair (unique both
sides — the suggester ranks id-like columns first), same-named columns
pre-mapped; toast reports unmatched columns.

### 5.2 Reconcile 1:1 — happy path (derived column) ✅

Paste into the Mapping spec box, then **Run reconciliation**:

```json
{
  "name": "customers_11", "cardinality": "1:1",
  "source_a": "cust_src", "dataset_a": "customers_src.csv",
  "source_b": "stg", "dataset_b": "stg_customers.csv",
  "key_pairs": [{"src": "customer_id", "tgt": "customer_id"}],
  "columns": [
    {"tgt": "full_name", "transform": "first_name + ' ' + last_name"},
    {"src": "email", "tgt": "email"},
    {"src": "segment", "tgt": "segment"}]
}
```

✅ **PASS** — 8 → 8 rows, 7/7 checks. `value_match` computed `full_name` on
the source (`first_name + ' ' + last_name`) and matched all 8 rows.

### 5.3 Reconcile 1:1 — missing / orphan / wrong value ✅

Same spec but `"source_b": "stg_bad", "dataset_b": "stg_customers_bad.csv"`.

✅ **FAIL**: `missing_in_target` = key **106**; `orphans_in_target` = key
**999**; `value_match:full_name` = 1 diff on key **103**
(`Cara Diaz` vs `C X`).

### 5.4 Reconcile 1:1 — derived calc + aggregate parity ✅

Source A `ord_src`, B `ord_stg`. Spec: cardinality `1:1`, key `order_id`,
columns `[{"tgt":"order_total","transform":"qty * unit_price","tolerance":0.01}]`,
aggregates `[{"src":"order_total","tgt":"order_total","op":"sum","tolerance":0.5}]`.

✅ **PASS** — 12/12 matches; `aggregate:sum(order_total)` **463.29 vs 463.29**.
Against `ord_stg_bad` → **FAIL**: missing **205**, orphan **999**, value diff
on 207 (99.99 vs 9.99), aggregate **463.29 vs 361.79** (|diff| 101.5).

### 5.5 Reconcile 1:N — orders → lines ✅

A `ord_src` vs B `lines`: cardinality `"1:N"`, key_pairs `order_id→order_id`,
columns `[]`.

✅ **PASS** — source keys unique; 12 repeated target keys reported as
expected for 1:N; 12 → 24 row count informational.

### 5.6 Reconcile N:1 — transactions → daily summary ✅

A `tx` vs B `ds`: cardinality `"N:1"`, key_pairs `txn_date→txn_date`,
columns `[]`, aggregates
`[{"src":"amount","tgt":"total_amount","op":"sum","tolerance":0.01}]`.

✅ **PASS** — target keys unique, source repeats expected,
`sum(amount->total_amount)` **3148.75 vs 3148.75**.
Against `ds_bad` → aggregate **FAIL**: 3148.75 vs 3248.75 (|diff| 100).

### 5.7 M:N (many-to-many) ✅

Cardinality `"M:N"` declares that **both** sides may repeat keys — the
junction-table pattern (e.g. students ↔ courses: one student takes many
courses, one course has many students). The sample files
`enroll_src.csv` / `enroll_tgt.csv` model exactly that: `student_id` S1 and
S3 each appear twice **and** `course_id` M101 appears three times, so no
single column is unique on either side — only the *composite*
(student_id + course_id) is.

Register `enr_src` → `samples/enroll_src.csv`,
`enr` → `samples/enroll_tgt.csv`, `enr_bad` →
`samples/enroll_tgt_bad.csv`. Then run this spec (note `key_pairs` has
**two** entries — that is how a composite key is declared):

```json
{
  "name": "enroll_mn", "cardinality": "M:N",
  "source_a": "enr_src", "dataset_a": "enroll_src.csv",
  "source_b": "enr", "dataset_b": "enroll_tgt.csv",
  "key_pairs": [{"src": "student_id", "tgt": "student_id"},
                {"src": "course_id", "tgt": "course_id"}],
  "columns": []
}
```

✅ **PASS** — 5 → 5 rows. The uniqueness checks report the repeated keys as
*expected for M:N* (severity info) instead of failing — run the identical
spec with `"cardinality": "1:1"` instead and watch it **FAIL** on duplicate
keys, then switch back to `"M:N"`.

**Negative case** — change the spec to `"source_b": "enr_bad",
"dataset_b": "enroll_tgt_bad.csv"` (the bad target drops `(S3, M103)` and
adds `(S9, M999)`) → **Run reconciliation**:

✅ **FAIL** — `missing_in_target` shows key `(S3, M103)` and
`orphans_in_target` shows `(S9, M999)`. This proves M:N relaxes
*uniqueness* but still enforces **completeness** — a dropped junction row
is caught.

### 5.8 Save, reuse, delete a mapping ✅

1. With a working spec in the editor, click **Save mapping** → reload the
   page → pick the name in the **saved mappings…** dropdown.

✅ The spec JSON and both dataset selections are restored automatically.

2. With that mapping still selected in the dropdown, click **Delete** →
   confirm.

✅ It disappears from the dropdown and stays gone after a page reload.

---

## 6. Schema gate — break the batch on schema drift

### 6.1 UI: pin + gate ✅

Select `cust_src`/`customers_src.csv` in the sidebar → Reconcile tab →
Schema gate → **Pin baseline** → **Run gate**.

✅ *"Pinned baseline v1 (5 columns)"*; gate **PASS — schema compatible with
baseline v1**.

**Bonus — make the gate FAIL without leaving the UI.** The gate always
compares the dataset's *current* contents against its baseline, so:

1. In a terminal: `copy samples\customers_src.csv samples\gate_input.csv`
   (Linux/macOS: `cp`).
2. Register `gate_src` → `samples/gate_input.csv` → select it → **Pin
   baseline** (v1 captured).
3. `copy /y samples\customers_v2_breaking.csv samples\gate_input.csv`
   — a "new extract" landed under the same filename.
4. Back in the UI with `gate_src`/`gate_input.csv` still selected →
   **Run gate**.

✅ **FAIL — 2 breaking change(s) vs baseline v1**: `column_removed segment`
and `type_changed customer_id numeric → categorical` — same verdict as the
CLI in 6.2.

5. `copy /y samples\customers_v3_compatible.csv samples\gate_input.csv` →
   **Run gate** again.

✅ **PASS** — one `compatible column_added loyalty_tier` row.

### 6.2 CLI: breaking change fails ✅

```
copy samples\customers_src.csv samples\gate_input.csv     :: Linux: cp
datavigil schema pin samples/gate_input.csv
datavigil gate samples/gate_input.csv                     :: PASS, exit 0
copy /y samples\customers_v2_breaking.csv samples\gate_input.csv
datavigil gate samples/gate_input.csv                     :: FAIL, exit 1
```

✅ Gate #2 lists two breaking changes — `customer_id type_changed
numeric→categorical`, `segment column_removed` — exits **code 1** (this is
what stops an ECS/CI batch before bad data lands).

### 6.3 CLI: compatible change passes ✅

```
copy /y samples\customers_v3_compatible.csv samples\gate_input.csv
datavigil gate samples/gate_input.csv
```

✅ One compatible change — `loyalty_tier column_added` — **PASS**, exit 0.
`datavigil schema versions samples/gate_input.csv` lists v1.

### 6.4 Diff + versions in the UI ✅

1. Select `cust_src`/`customers_src.csv` (the pinned dataset) → Reconcile
   tab → Schema gate.
2. The line under the buttons shows the pinned versions — e.g.
   *"baselines for samples\customers_src.csv: v1 (5 cols)"* (or *"no
   baseline pinned … yet"* before the first pin).
3. **Run gate** — the result table lists each change under its
   classification: **breaking** (removed column/type change), **warning**
   (dtype drift), or **compatible** (added column) — the same classification
   `GET /api/schema/diff` returns to integrations.

---

## 7. Batch execution

### 7.1 Sample manifest — from the UI ✅

1. Open the **Reconcile** tab and scroll to **Batch — run a manifest** —
   a JSON manifest equivalent to `samples/batch_manifest.yml` is already
   filled in.
2. Click **Run batch**.

✅ A **PASSED** banner with *"0 / 6 jobs failed"* and a table of all six
jobs — the gate, the four reconcile pairs (customers, orders, 1:N lines,
N:1 daily) and the parts audit — each row green **pass**. The batch also
auto-registers the `batch_*` file sources, so they appear in the source
dropdown afterwards (remove them with **Remove source** when done).

### 7.2 Failure propagation ✅

In the manifest textarea, change `customers-to-stg`'s `target_file` to
`"samples/stg_customers_bad.csv"` → **Run batch**.

✅ Banner shows **FAILED — 1 / 6 jobs failed**; the `customers-to-stg` row
is red **fail** while the others pass. (`"fail_fast": true` in the manifest
stops at the first failure.)

### 7.3 CLI equivalent ✅

```
datavigil batch samples/batch_manifest.yml
```

✅ Same six jobs — `batch PASSED — 0/6 jobs failed`, exit 0 (exit 1 on any
failure — that's what CI/ECS uses).

---

## 8. Audit & Report

### 8.1 Single table ✅

Select `parts_inventory.csv` → **Audit & Report** → **Audit this table**.

✅ Cards (rows, cols, DQ score, rules passed, anomalies) + analysis block +
recent-runs history table.

### 8.2 Whole schema ✅

Select a multi-dataset source — e.g. the `folder_src` you registered in 1.7
(the `samples` folder, ~20 datasets) — → **Audit whole schema**. This takes
roughly half a minute on the samples folder; the button stays disabled
while it runs.

✅ Cards: tables audited, avg/worst score, failed rules, anomalies;
worst-first table list. Files that cannot be profiled (e.g. `custcopy.dat`
without its copybook, `.sql` scripts) show as per-table errors — they are
listed, not fatal; the run still completes. Parallel
(`parallel_workers=4`, cap `audit_max_tables=50`). After it finishes, the
**Download schema DOCX / PDF** buttons appear — verify both download.

### 8.3 Export reports ✅

**Download DOCX / PDF** (per-table) and **Download schema DOCX / PDF**
(whole-schema buttons appear after a schema audit).

✅ Files download and land in `~/.datavigil/reports/` — profile, score,
failed rules with examples, anomalies, analysis.

---

## 9. Database schema testing — every feature on Aurora

This section runs the **same features** against a real database so you can
verify nothing is file-only. Run `samples/sql/aurora_setup.sql` once — it
creates schema `demo` mirroring the CSV samples (so all expected numbers
match), plus planted issues in `products`.

```
psql -h <cluster-endpoint> -U <user> -d <database> -f samples/sql/aurora_setup.sql
```

✅ Setup check: the final query lists 14 tables — `src_customers` (8),
`stg_customers` (8), `stg_customers_bad` (8), `src_orders` (12),
`stg_orders` (12), `stg_orders_bad` (12), `order_lines` (24), `txn` (20),
`daily_summary` (4), `daily_summary_bad` (4), `enroll_src` (5),
`enroll_tgt` (5), `enroll_tgt_bad` (5), `products` (204).

### DB-1 Connect (four variants) ✅

- **Direct**: **connect a database** → name `aurora`, dialect **Aurora
  PostgreSQL (AWS RDS)**, host/port/user/password/database, schema `demo`
  → Connect. `sslmode=require` is added automatically.
- **IAM auth**: tick **AWS IAM authentication** — password greys out, fill
  the region; a short-lived RDS token is generated (needs `boto3` + IAM
  `rds-db:connect` on the cluster user).
- **MCP / Data API**: **Aurora via MCP** → method Data API → cluster ARN,
  Secrets Manager ARN, database, region, schema `demo`. Works without a
  network path to the DB.
- **MCP / pgwire**: method pgwire → same fields + DB endpoint.

✅ *"Connected 'aurora' — 14 datasets"*; `demo.*` tables appear in the
dataset list.

### DB-2 Profile a DB table ✅

Select `products` → Profile.

✅ **204 rows** (200 + 4 planted duplicates → Duplicate-rows card = 4);
`supplier` ~6% null; `unit_cost` shows **6 neg**; `supplier_email` ~8% null
with the email pattern on the rest. Ticking **AI insights** produces the
same insight block as on files (needs LLM).

### DB-3 Rules on a DB table ✅

With `products` selected → Rules & Score → Suggest → Run.

✅ Score appears exactly like the file run; the ruleset is stored under
`aurora.products` — **independent of every file dataset's rules**
(re-select a file dataset to confirm its own rules are unchanged).

With an LLM configured, the AI rule features work identically on the DB
table: **Add via AI** (*"unit_cost must be positive"*) adds an expression
rule that fails exactly the 6 planted negatives; **Explain failures**
returns root causes for the `aurora.products` ruleset.

### DB-4 Anomalies on a DB table ✅

✅ The 5 planted price outliers (`product_id` 33, 77, 121, 166, 189) are
flagged by IQR / isolation forest.

### DB-5 Reconcile table → table ✅

Same Reconcile tab, but both sides are Aurora datasets — reuse the specs
from section 5 verbatim except `source_a`/`source_b` = `aurora` and the
`dataset_*` values become the **table names** (no `.csv` suffix):

| Case | A → B | Expected |
|------|-------|----------|
| 1:1 derived | `src_customers` → `stg_customers` (spec of 5.2) | **PASS**, 8/8, derived `full_name` |
| 1:1 bad | `src_customers` → `stg_customers_bad` | **FAIL**: missing 106, orphan 999, name diff on 103 |
| 1:1 + aggregate | `src_orders` → `stg_orders` (spec of 5.4) | **PASS**, `sum(order_total)` 463.29 vs 463.29 |
| 1:1 + aggregate bad | `src_orders` → `stg_orders_bad` | **FAIL**: missing 205, orphan 999, diff on 207, agg 463.29 vs 361.79 |
| 1:N | `src_orders` → `order_lines` (spec of 5.5) | **PASS**, 12 repeated keys expected, 12→24 rows |
| N:1 aggregate | `txn` → `daily_summary` (spec of 5.6) | **PASS**, `sum(amount->total_amount)` 3148.75 vs 3148.75 |
| N:1 aggregate bad | `txn` → `daily_summary_bad` | aggregate **FAIL**: 3148.75 vs 3248.75 |
| M:N junction | `enroll_src` → `enroll_tgt` (spec of 5.7, composite key) | **PASS**, 5→5, repeats expected |
| M:N junction bad | `enroll_src` → `enroll_tgt_bad` | **FAIL**: missing (S3,M103), orphan (S9,M999) |

✅ Identical results to the file runs — the engine is source-agnostic.

### DB-6 Reconcile file → table (cross-source) ✅

A = file `cust_src`/`customers_src.csv`, B = `aurora`/`stg_customers`, same
spec as 5.2.

✅ Same **PASS** — a file and a database table reconcile identically.

### DB-7 Schema gate on a live table ✅ — *the breaking-change scenario*

1. Select `aurora`/`src_customers` → Schema gate → **Pin baseline** →
   **Run gate** → PASS (v1, 5 columns).
2. In psql, change the schema:

   ```sql
   ALTER TABLE demo.src_customers DROP COLUMN segment;
   ```

3. **Run gate** again.

   ✅ **FAIL** — `segment column_removed` classified **breaking**.

4. Restore and try a type flip:

   ```sql
   ALTER TABLE demo.src_customers ADD COLUMN segment varchar(10);
   ALTER TABLE demo.src_customers ALTER COLUMN customer_id TYPE varchar(10)
       USING customer_id::text;
   ```

5. **Run gate**.

   ✅ **FAIL** — `customer_id type_changed` breaking (this is the
   string↔int class of change that stops an ingestion batch).

6. Version history works on the DB dataset too — the line under the gate
   buttons lists the pinned versions for `src_customers`, and each gate
   run's result table shows the breaking/warning/compatible
   classification.

7. Restore the demo schema by re-running `aurora_setup.sql`.

> The same gate works via CLI/CI on any registered source — it profiles the
> table *now* and diffs against the pinned baseline.

### DB-8 Audit a table, then the whole schema ✅

1. Select `aurora`/`products` → **Audit this table** → audit cards render
   for the DB table; **Download DOCX / PDF** produce the same reports as
   for files.
2. Select source `aurora` (no dataset needed) → **Audit whole schema**.

✅ All 14 `demo` tables audited in parallel; score-by-table list
worst-first — expect the `*_bad` staging tables and `products` to score
lowest. Download **schema DOCX/PDF** → one consolidated report.

### DB-9 Compare two tables ✅

Reconcile tab → Source A: `aurora` / `src_orders`; Source B: `aurora` /
`stg_orders` → **Compare schemas**.

✅ Cards show `qty` and `unit_price` under **Only in A** (red chips) and
`order_total` under **Only in B** (green chip) — the derived-column drift
between source and staging is visible without any mapping. All shared
columns show **same**.

### DB-10 Rules history on a DB table ✅

Run & score on `aurora`/`products`, then open the **Audit & Report** tab
history table → the run appears with dataset `products`.

### DB-11 Batch against DB tables ✅

Batch jobs can reference registered DB datasets by `source`/`dataset`
instead of `source_file`. In the Reconcile tab's **Batch** box, replace the
manifest with this and click **Run batch**:

```json
{
  "parallelism": 2,
  "jobs": [
    { "name": "orders-pair", "type": "pair",
      "source": "aurora", "dataset": "src_orders",
      "target_source": "aurora", "target_dataset": "stg_orders",
      "mapping": {
        "cardinality": "1:1",
        "key_pairs": [{"src": "order_id", "tgt": "order_id"}],
        "columns": [{"tgt": "order_total", "transform": "qty * unit_price"}],
        "aggregates": [{"src": "order_total", "tgt": "order_total",
                        "op": "sum", "tolerance": 0.5}] } },
    { "name": "customers-gate", "type": "gate",
      "source": "aurora", "dataset": "src_customers",
      "pin_if_missing": true }
  ]
}
```

✅ Both jobs run against the live tables: `orders-pair` green **pass**
(sum 463.29 parity), `customers-gate` passes or fails depending on whether
DB-7 left the schema altered — a real gate on real table state.

### DB-12 Ask on a DB dataset (AI only) ✅

Select `aurora`/`products` → **Ask** tab → *"Which columns have the worst
data quality?"*

✅ Answer references `products` columns (supplier nulls, negative costs,
price outliers) — Q&A works on database datasets exactly as on files.

---

## 10. Ask (AI only)

With an LLM configured → **Ask** tab → *"Which columns have the worst data
quality and why?"*

✅ Plain-language answer referencing real profile findings.

---

## 11. CLI quick reference

```
datavigil serve [--host H --port P]
datavigil profile samples/parts_inventory.csv [--insights]
datavigil run samples/parts_inventory.csv --fail-under 80
datavigil anomalies samples/parts_inventory.csv
datavigil audit samples/parts_inventory.csv --no-ai
datavigil audit DIR --all                 # every file in a folder
datavigil report samples/parts_inventory.csv --format pdf
datavigil profile FILE --options '{"sep":"|"}'
datavigil schema pin|versions|diff FILE   # baseline + history
datavigil gate FILE                       # exit 1 on breaking schema
datavigil reconcile SPEC.json             # inline or saved mapping
datavigil batch samples/batch_manifest.yml
datavigil llm-health
```

> **If `datavigil --help` does not list `schema`, `gate`, `reconcile` or
> `batch`, the installed copy is stale** — an editable install may be
> pointing at an older checkout. Repoint it from the repo root:
> `.venv\Scripts\pip.exe install -e .` (Linux/macOS:
> `.venv/bin/pip install -e .`), then `--help` again.

---

## Feature coverage checklist

| Feature | File test | DB test |
|---|---|---|
| Upload / register source | 1.1–1.7 | DB-1 |
| Reader options / delimiters / FWF / NDJSON | 1.2–1.5 | — |
| Copybook (EBCDIC/COMP-3) | 1.6 | — |
| DB connect direct / IAM / MCP×2 | — | DB-1 |
| Profiling + AI insights | 2.1–2.2 | DB-2 |
| Compare / schema drift (UI button) | 2.3 | DB-9 |
| Suggested + per-table rules | 3.1–3.2 | DB-3 |
| NL rules / explain failures | 3.3–3.4 | DB-3 |
| Run history / per-dataset rulesets | 3.5–3.6 | DB-10 |
| Anomaly detection / realtime scorer | 4.1–4.2 | DB-4 |
| Reconcile 1:1 / 1:N / N:1 / M:N | 5.2–5.7 | DB-5 |
| Derived columns + aggregates | 5.2, 5.4, 5.6 | DB-5 |
| Missing/orphan/key checks | 5.3 | DB-5 |
| Cross-source reconcile | — | DB-6 |
| Mapping save/reuse/delete (UI) | 5.8 | 5.8 (DB specs work identically) |
| Schema gate pin/diff/versions | 6.1–6.4 | DB-7 |
| Breaking schema fail (CLI + live DB) | 6.2 | DB-7 |
| Batch manifest — UI + CLI | 7.1–7.3 | DB-11 |
| Audit table / whole schema | 8.1–8.2 | DB-8 |
| DOCX + PDF export | 8.3 | DB-8 |
| Ask / AI config / theme | 0, 10 | DB-12 |
| REST API surface (/docs) | — | — |
| CLI | 11 | — |

---

## Reporting a problem

If any ✅ expected result does not match, note:
- which step number, expected vs. actual,
- a screenshot, and
- the text in the console window if the app showed an error.
