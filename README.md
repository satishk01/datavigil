# DataVigil

An independent, open-source **data quality & profiling platform** — a free,
self-hostable  built for
**non-technical (black-box) testers**: everything is one click in the browser,
no SQL or coding required.

## Five pillars

| Pillar | What it does |
|--------|--------------|
| **Profiling** | Table/column profiling — null %, types, min/max/mean/stddev, distinct counts, value distributions, pattern detection (email, dates, structural masks), correlation analysis, auto type classification. |
| **Rules & Scoring** | A rule engine across DQ dimensions (completeness, uniqueness, validity, consistency, timeliness…), per-run execution with failed examples, and a weighted **0–100 DQ score** with per-dimension breakdown. Rule history is persisted for trends. |
| **Anomaly detection** | Statistical (z-score, IQR) + ML (Isolation Forest) + rare-category + pattern-break detection, plus a **real-time scorer** for streaming rows. |
| **AI / NL interface** | Describe a check in plain English → validated rules. Plain-language profile insights. Conversational Q&A. **AI root-cause analysis** explains every failed rule and recommends fixes. |
| **Audit & Reporting** | One-click **full audit** (profile → rules → score → anomalies → AI analysis) for a single table **or every table in a schema**, and exportable **DOCX / PDF reports** you can attach to a bug or share with stakeholders. |

## Quick start

```bash
# 1. install (core)
pip install -e .
# with database drivers:  pip install -e ".[all]"

# 2. launch the dashboard
datavigil serve            # → http://127.0.0.1:8000

# 3. or use the CLI (great for CI / scheduled jobs)
datavigil profile samples/parts_inventory.csv
datavigil run     samples/parts_inventory.csv --fail-under 80
datavigil anomalies samples/parts_inventory.csv
```

## Data sources

- **Files**: CSV, TSV, delimited text (`.txt`/`.dat` with any delimiter — `|` `;` `:` etc.),
  Excel (per-sheet), Parquet, JSON, JSON Lines (`.jsonl`/`.ndjson`), and fixed-width
  (`.fwf`) — upload in the UI or pass a path.
  Reader options (delimiter, encoding, header, widths…) are set per source:
  in the UI use the *reader options* box under the file picker; via API/CLI pass
  `options` params, e.g. `{"sep": "|", "decimal": ","}` or `--options '{"sep":"|"}'`.
- **IMS/COBOL copybooks**: fixed-length mainframe records (EBCDIC `cp037` or ASCII)
  described by a copybook — PIC X/9/S9, COMP-3 packed decimal, COMP binary,
  OCCURS, REDEFINES, FILLER, 88-levels, multiple 01 record layouts.
  Register as source type `copybook` with `path` (data file) + `copybook`
  (layout file). Each 01-level becomes a dataset.
- **Aurora PostgreSQL (AWS RDS)** — first-class support for **provisioned and
  serverless** clusters, three ways:
  - **Direct** (SQLAlchemy + psycopg2): pick "Aurora PostgreSQL" in the connect
    form; `sslmode=require` is added automatically. Tick **IAM authentication**
    to use a generated RDS token instead of a password (needs `boto3` +
    IAM-granted `rds-db:connect`). Works on every Aurora cluster.
  - **Via MCP / RDS Data API** (`rdsapi`): uses the
    [`awslabs.postgres-mcp-server`](https://github.com/awslabs/mcp) MCP server.
    **No network path to the DB needed** — queries go over the RDS Data API
    HTTPS endpoint. On provisioned clusters you must first enable it:
    `aws rds enable-http-endpoint --resource-arn <cluster-arn>`
    (or the cluster Actions menu in the console) — plus a Secrets Manager
    secret with the DB credentials and IAM perms `rds-data:ExecuteStatement` +
    `secretsmanager:GetSecretValue`. Supported engine: Aurora PG ≥ 13.11 /
    14.8 / 15.3 / 16.1 / 17.4 / 18.3.
  - **Via MCP / pgwire**: same MCP server but a direct wire connection —
    pick this if Data API is off or can't be enabled. Needs the DB endpoint
    reachable + a Secrets Manager ARN with username/password.
  - The MCP path needs `pip install mcp` and either `uv` (uvx) or
    `pip install awslabs.postgres-mcp-server`.
- **Other databases** (SQLAlchemy): PostgreSQL, MySQL/MariaDB, SQL Server, Oracle, Snowflake, SQLite.
  Connect with host/user/password or a full SQLAlchemy URL. Add a driver from the
  `optional-dependencies` extras for your database.

## AI configuration

Set the provider once in the UI (⚙ button, top-right) — no env files needed for
testers. Or via environment / `~/.datavigil/config.yaml`:

```bash
# AWS Bedrock (primary) — Nova Pro, Claude, Llama… any enabled model id
export DATAVIGIL_LLM_PROVIDER=bedrock
export DATAVIGIL_LLM_MODEL=us.amazon.nova-pro-v1:0
export DATAVIGIL_LLM_REGION=us-east-1          # or AWS_REGION / AWS_DEFAULT_REGION

# Bedrock auth — pick one (DATAVIGIL_LLM_AWS_AUTH):
#   profile      default AWS chain / named profile (env, ~/.aws, SSO, IAM role)
export DATAVIGIL_LLM_AWS_AUTH=profile
export DATAVIGIL_LLM_AWS_PROFILE=myprofile     # optional named profile
#   keys         long-term access key + secret (session token optional)
export DATAVIGIL_LLM_AWS_ACCESS_KEY_ID=AKIA…
export DATAVIGIL_LLM_AWS_SECRET_ACCESS_KEY=…
#   keys_session short-term keys — same + required session token
export DATAVIGIL_LLM_AWS_SESSION_TOKEN=…
#   api_key      Bedrock API key (bearer token — no SigV4 creds needed)
export DATAVIGIL_LLM_BEDROCK_API_KEY=…

# Other providers:
#   DATAVIGIL_LLM_PROVIDER=ollama   DATAVIGIL_LLM_BASE_URL=http://localhost:11434  DATAVIGIL_LLM_MODEL=llama3.1
#   DATAVIGIL_LLM_PROVIDER=openai / azure_openai + DATAVIGIL_LLM_API_KEY=…
#   DATAVIGIL_LLM_PROVIDER=none     (AI features off; everything else still works)
```

The NL-to-rule translator is **constrained to the rule registry** and validated
before execution, so a hallucinated rule type can never run.

## For black-box testers

1. `Start-DataVigil.bat` → browser opens.
2. Upload a file **or** connect to Aurora (direct or "via AWS (MCP)").
3. Pick a table in the **Datasets** list.
4. **Audit & Report** tab → **Audit this table** (or **Audit whole schema** to check every table in the source at once) → **Download DOCX / PDF**.

See [BLACK_BOX_TEST_GUIDE.md](BLACK_BOX_TEST_GUIDE.md) for the full walkthrough,
or **[test-steps.pdf](test-steps.pdf)** — a printable test plan covering
every feature with expected results and a sign-off matrix (regenerate with
`python docs/build_test_steps_pdf.py`).

### Sample data

`samples/` contains deterministic test data for every feature — CSV,
pipe/semicolon-delimited text, NDJSON, fixed-width, an IMS/COBOL copybook
(`custcopy.cpy` + `custcopy.dat`, EBCDIC with COMP-3), good/bad reconcile
pairs for 1:1, 1:N and N:1 cardinalities, schema-gate variants
(`customers_v2_breaking.csv`, `customers_v3_compatible.csv`), a batch
manifest, and `samples/sql/aurora_setup.sql` which creates an Aurora
PostgreSQL `demo` schema mirroring the files. Regenerate all file samples
with `python samples/generate_samples.py`. See `samples/README.md` for the
full matrix.

## Reconciliation & schema contracts

- **Mappings** (`Reconcile` tab / `PUT /api/mappings/{name}`): declare how a
  source dataset maps to a target — key pairs, column renames, derived
  columns (`transform` expressions like `first + ' ' + last` or
  `qty * price`), declared **cardinality** (`1:1`, `1:N`, `N:1`, `M:N`), and
  aggregate checks (`sum`/`count`/`min`/`max`/`mean` with tolerance).
- **Reconcile** runs the checks: schema presence, row counts, key
  uniqueness per cardinality, missing/orphan keys, row-level value match on
  joined keys, and aggregate parity — returning pass/fail plus examples.
- **Schema gate**: pin a dataset's schema as a versioned baseline
  (`datavigil schema pin FILE`), then `datavigil gate FILE` on each new
  extract. Removed columns and type changes (e.g. `customer_id` string →
  int) are classified **breaking** and exit non-zero — wire it into an
  ingestion script or scheduled job to break the batch before the load.
- **Batch**: `datavigil batch manifest.yml` runs a YAML manifest of
  `audit` / `reconcile` / `gate` / `pair` jobs in parallel and fails the
  process if any job fails — built for CI and scheduled batch windows.

## CLI reference

```
datavigil serve [--host H --port P]      launch dashboard + API
datavigil profile PATH [--insights]      profile a file, print JSON
datavigil run PATH [--fail-under N]      suggest+run rules, exit non-zero if score < N
datavigil anomalies PATH [--methods ...] detect anomalies
datavigil audit   PATH [--no-ai] [--all]  one-shot audit summary (per file or --all = whole source)
datavigil report  PATH --format docx|pdf [--out DIR] [--all]   export a report file
datavigil reconcile NAME|SPEC.json       run a saved or inline source->target reconciliation
datavigil schema pin PATH                pin current schema as baseline vN
datavigil schema versions|diff PATH      list baselines / diff vs latest
datavigil gate   PATH [--pin-if-missing] fail (exit 1) on breaking schema changes
datavigil batch  MANIFEST.yml [--parallel N] [--fail-fast]   run a job manifest
datavigil llm-health                     check the configured LLM
```

All file commands accept `--options '{"sep":"|"}'` for reader options.

## Architecture

```
connectors/   source abstraction (files, copybook, SQLAlchemy DBs, Aurora/MCP Data API)
profiling/    profiler + pattern inference
rules/        rule registry, engine+scoring, auto-suggester
reconcile/    source->target reconciliation (keys, values, cardinality, aggregates)
anomaly/      statistical + ML detectors, real-time scorer
llm/          pluggable provider (bedrock/ollama/openai/azure), NL→rules,
              insights, Q&A, failure root-cause analysis
report/       DOCX + PDF report renderers
core/         models, config, schema contracts, batch runner, DataVigilService orchestrator
api/          FastAPI app (REST + dashboard)
web/          dashboard (HTML/CSS/JS)
cli.py        Typer CLI
```

Everything flows through `DataVigilService` (`core/service.py`) — the API, CLI, and
tests all use the same facade, so new surfaces are thin.

## Tests

```bash
pytest -q
```

## Extending

- **New rule type**: add a `@rule(...)`-decorated function in `rules/registry.py`.
  It is automatically available to the engine, the suggester, the NL translator, and the UI.
- **New connector**: implement `Connector` (`list_datasets` + `load`) and register it in
  `connectors/__init__.py`.
- **New LLM provider**: add a subclass in `llm/provider.py`.

