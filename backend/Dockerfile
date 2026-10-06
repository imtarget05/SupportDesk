FROM python:3.12-slim AS builder

WORKDIR /srv

# Install build dependencies for compiling psycopg2-binary and other C extensions
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libpq-dev python3-dev \
    && rm -rf /var/lib/apt/lists/*

COPY backend/requirements.txt .
RUN pip install --no-cache-dir --user -r requirements.txt

# ── Production stage ──────────────────────────────────────────────────────────
FROM python:3.12-slim AS production

WORKDIR /srv/backend

# Copy installed packages from builder
COPY --from=builder /root/.local /srv/.local

# Copy application code
COPY backend/ ./
# Tests resolve the repo root at /srv (backend's parent), so absolute paths:
COPY evaluation/ /srv/evaluation/
COPY frontend/nginx.conf /srv/frontend/nginx.conf

# Runtime system deps only (libpq for psycopg2, curl for healthcheck if needed)
RUN apt-get update && apt-get install -y --no-install-recommends \
    libpq5 curl \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN useradd -m -u 1000 appuser && \
    chown -R appuser:appuser /srv

ENV PATH=/srv/.local/bin:$PATH \
    PYTHONPATH=/srv/backend:/srv/.local/lib/python3.12/site-packages \
    PORT=8000

USER appuser

ENV AI_PROVIDER=stub \
    AI_EMBED_PROVIDER=bow

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=5s --retries=3 \
  CMD python -c "import os, urllib.request; urllib.request.urlopen('http://localhost:' + os.getenv('PORT', '8000') + '/api/health')" || exit 1

CMD ["sh", "-c", "exec uvicorn app.main:app --host 0.0.0.0 --port \"${PORT:-8000}\" --proxy-headers --forwarded-allow-ips=\"*\""]
