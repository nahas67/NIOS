# NIOS Core v3.0 production image -- React 19 bundle + Python 3.12 runtime.
# Build:  docker build -f docker/Dockerfile -t nios-core:3.0.0 .
FROM node:24-alpine AS ui
WORKDIR /ui
COPY ring3_cockpit/ui/openalgo_shell/package.json ring3_cockpit/ui/openalgo_shell/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY ring3_cockpit/ui/openalgo_shell/ ./
RUN npm run build

FROM python:3.12-slim AS runtime
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    NUMBA_CACHE_DIR=/tmp/numba \
    NIOS_HOST=0.0.0.0 \
    NIOS_PORT=8000
WORKDIR /app

# Runtime stack per AGENTS.md section 9: FastAPI, DuckDB, Polars/Numba, NATS.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl \
    && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir \
        "fastapi>=0.115" \
        "uvicorn[standard]>=0.30" \
        "pydantic>=2.8" \
        "numpy>=1.26" \
        "polars>=1.0" \
        "duckdb>=1.0" \
        "numba>=0.60" \
        "pandas>=2.2" \
        "scipy>=1.13" \
        "httpx>=0.27" \
        "PyYAML>=6.0"

COPY . /app
COPY --from=ui /ui/dist /app/ring3_cockpit/ui/openalgo_shell/dist

# Non-root runtime user; /dev/shm and the ring0 volume are mounted by compose.
RUN useradd --create-home --uid 10001 nios \
    && chown -R nios:nios /app
USER nios

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/v2/cockpit/health', timeout=3)"

CMD ["python", "scripts/run_nios.py", "--host", "0.0.0.0", "--port", "8000"]
