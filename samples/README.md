# Sample data pack

Everything needed to run the test plan in `test-steps.pdf`. Regenerate any
file with `python samples/generate_samples.py`.

| File | Shape | Used for |
|------|-------|----------|
| `parts_inventory.csv` | 2000 × 9 | Profiling, rules, anomalies, audit — nulls, negatives, dup checks, correlation |
| `customers_src.csv` | 8 × 5 | Reconcile source; schema-gate baseline |
| `stg_customers.csv` | 8 × 4 | Reconcile 1:1 target (full_name = first + last) — PASS expected |
| `stg_customers_bad.csv` | 8 × 4 | Same, with missing 106, orphan 999, wrong name on 103 — FAIL expected |
| `orders_src.csv` | 12 × 6 | Reconcile source (order_total = qty × unit_price) |
| `stg_orders.csv` | 12 × 5 | 1:1 target + `sum(order_total)` aggregate (463.29) — PASS |
| `stg_orders_bad.csv` | 12 × 5 | Missing 205, orphan 999, wrong total on 207 — FAIL |
| `order_lines.csv` | 24 × 4 | 1:N fanout target (2 lines per order) |
| `txn.csv` | 20 × 3 | N:1 source (transactions per day) |
| `daily_summary.csv` | 4 × 3 | N:1 target — `sum(amount→total_amount)` = 3148.75 — PASS |
| `daily_summary_bad.csv` | 4 × 3 | +100 on 2024-07-03 — aggregate FAIL |
| `parts_pipe.txt` | 30 × 3 | Pipe-delimited text (auto-sniffed or `{"sep":"|"}`) |
| `parts_semicolon.csv` | 30 × 2 | Semicolon delimiter + comma decimals `{"sep":";","decimal":","}` |
| `customers.ndjson` | 8 × 5 | JSON Lines |
| `customers_fwf.fwf` | 8 × 4 | Fixed-width — `{"widths":[4,12,12,8],"names":["id","first","last","segment"]}` |
| `custcopy.cpy` + `custcopy.dat` | 3 × 8 | IMS/COBOL copybook source (EBCDIC, COMP-3, OCCURS, REDEFINES) |
| `customers_v2_breaking.csv` | 8 × 4 | Schema-gate BREAKING: customer_id→string, segment dropped |
| `customers_v3_compatible.csv` | 8 × 6 | Schema-gate COMPATIBLE: adds loyalty_tier |
| `batch_manifest.yml` | 6 jobs | `datavigil batch samples/batch_manifest.yml` |
| `sql/aurora_setup.sql` | 11 tables | Aurora/Postgres demo schema `demo` — mirrors these files |

## Aurora quick start

```sql
psql -h <cluster-endpoint> -U <user> -d <db> -f samples/sql/aurora_setup.sql
```

Creates schema `demo`: `src_customers`, `stg_customers(_bad)`, `src_orders`,
`stg_orders(_bad)`, `order_lines`, `txn`, `daily_summary(_bad)`,
`products` (204 rows with planted DQ issues). Then register the Aurora source
in DataVigil with schema = `demo` and test file→table or table→table
reconciliation against these datasets.
