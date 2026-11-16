# syntax=docker/dockerfile:1
# Production image for the FastAPI backend.
#   docker build -f backend/Dockerfile -t llm-router-api:<git-sha> .        (build context = repository root)
# The root .dockerignore is an allowlist: only requirements.txt and backend/app/ reach this build;
# .env files, tests, benchmarks and caches never do. No secret or provider key is baked in: all
# configuration arrives as environment variables at runtime.

ARG PYTHON_IMAGE=python:3.13.18-slim-trixie

# ---------------------------------------------------------------- build stage: wheels -> venv
FROM ${PYTHON_IMAGE} AS build
ENV PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONDONTWRITEBYTECODE=1
RUN python -m venv /opt/venv
COPY requirements.txt /tmp/requirements.txt
# Exact pins, no dependency resolution beyond the lockstep list; `pip check` proves it is complete.
RUN /opt/venv/bin/pip install --no-deps --only-binary=:all: -r /tmp/requirements.txt
RUN /opt/venv/bin/pip check
# The runtime needs no installer: remove pip from the venv (smaller attack surface).
RUN /opt/venv/bin/python -m pip uninstall -y pip

# ---------------------------------------------------------------- runtime stage
FROM ${PYTHON_IMAGE} AS runtime
LABEL org.opencontainers.image.title="llm-router-api" \
      org.opencontainers.image.description="Distributed Multi-Model Router API (FastAPI)"

# Unprivileged, fixed UID/GID (no shell login, no home contents).
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin app

ENV PATH="/opt/venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    # Never read a .env file inside the container: configuration comes from the environment only.
    LLMR_DISABLE_ENV_FILE=1 \
    # Read-only root filesystem: anything a library caches goes to the /tmp tmpfs.
    HOME=/tmp \
    TMPDIR=/tmp \
    HOST=0.0.0.0 \
    PORT=8000

COPY --from=build /opt/venv /opt/venv
WORKDIR /app
COPY --chown=root:root backend/app /app/app
# Code is owned by root and not writable by the app user.
RUN chmod -R a-w /app

USER 10001:10001
EXPOSE 8000

# Liveness only: /health never touches Redis or PostgreSQL (readiness is /health/dependencies, token-protected).
HEALTHCHECK --interval=15s --timeout=3s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2).status == 200 else 1)"]

# One uvicorn process per container (each process has its own Prometheus registry; scale with containers).
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "1", "--no-server-header"]
