# syntax=docker/dockerfile:1
# Non-root, honours PORT, terminates on SIGTERM.
FROM python:3.12-slim AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /build

RUN pip install --no-cache-dir "hatchling>=1.25"

COPY pyproject.toml README.md ./
COPY backend/src ./backend/src
RUN pip wheel --no-cache-dir --no-deps --wheel-dir /wheels .


FROM python:3.12-slim AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    USE_FAKE_AGENTS=true

# Non-root from here on. Cloud Run runs as this user.
RUN groupadd --system --gid 10001 firstdue \
 && useradd --system --uid 10001 --gid firstdue --create-home firstdue

WORKDIR /app

COPY --from=builder /wheels /wheels
RUN pip install --no-cache-dir /wheels/*.whl && rm -rf /wheels

COPY --chown=firstdue:firstdue fixtures ./fixtures

USER firstdue
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
  CMD python -c "import os,urllib.request;urllib.request.urlopen(f'http://127.0.0.1:{os.environ[\"PORT\"]}/healthz').read()"

# exec form so uvicorn is PID 1 and receives SIGTERM directly; the lifespan
# shutdown hook then drains readiness before the process exits.
CMD ["sh", "-c", "exec uvicorn firstdue.api.app:create_app --factory --host 0.0.0.0 --port ${PORT} --no-access-log --timeout-graceful-shutdown 20"]
