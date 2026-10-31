# Rhythm AI Trader API — multi-stage build, non-root runtime.

FROM python:3.12-slim AS build
ENV PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1
WORKDIR /build
COPY pyproject.toml README.md ./
COPY src ./src
RUN python -m venv /opt/venv \
 && /opt/venv/bin/pip install --upgrade pip \
 && /opt/venv/bin/pip install .

FROM python:3.12-slim AS runtime
ENV PATH=/opt/venv/bin:$PATH \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    RHYTHM_API_HOST=0.0.0.0 \
    RHYTHM_API_PORT=8000 \
    RHYTHM_TRADING_MODE=RESEARCH_ONLY
RUN groupadd --system --gid 10001 rhythm \
 && useradd --system --uid 10001 --gid rhythm --no-create-home --shell /usr/sbin/nologin rhythm
COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY alembic.ini ./
USER rhythm
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=2).status == 200 else 1)"
CMD ["rhythm-api"]
