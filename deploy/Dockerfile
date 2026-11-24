# DataVigil — all-in-one image (dashboard server + CLI batch runner)
#
# Easiest local test — two commands from the repo root:
#   docker build -t datavigil -f deploy/Dockerfile .
#   docker run --rm -p 8000:8000 datavigil
# then open http://localhost:8000 — sample files are in /app/samples
# inside the container (register them via "local file / copybook").
#
# Keep state between runs:
#   docker run --rm -p 8000:8000 -v datavigil-state:/data datavigil
# Batch mode instead of the dashboard:
#   docker run --rm datavigil batch /app/samples/batch_manifest.yml
#
# EXTRAS build-arg selects pyproject extras. The default deliberately skips
# `sqlserver`/`oracle` — pyodbc needs the unixODBC system driver; add
# `apt-get install -y unixodbc-dev` below if you need them.
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATAVIGIL_HOME=/data

WORKDIR /app
COPY pyproject.toml README.md ./
COPY datavigil ./datavigil
# deterministic test data — lets the black-box test plan run inside the
# container with zero setup; remove this line for a production image
COPY samples ./samples

ARG EXTRAS="postgres,mysql,aurora,bedrock,mcp,reports,snowflake"
RUN pip install --upgrade pip \
 && pip install ".[${EXTRAS}]"

# app state (sources, rulesets, mappings, schema baselines, run history,
# reports, uploads) lives in DATAVIGIL_HOME — mount EFS or a volume here
RUN useradd -r -u 10001 datavigil && mkdir -p /data && chown datavigil /data
VOLUME ["/data"]
USER datavigil

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
  CMD python -c "import urllib.request;urllib.request.urlopen('http://127.0.0.1:8000/api/health')" || exit 1

# default: dashboard. Batch jobs override with e.g. ["batch","/data/m.yml"]
ENTRYPOINT ["datavigil"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
