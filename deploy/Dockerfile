# Two stages: a build stage that may compile wheels, and a runtime that never
# sees a compiler. argon2-cffi and psycopg both have C extensions; shipping the
# toolchain that built them would put gcc in the production attack surface for
# no benefit after the build.

# Pinned by digest, not by tag. `python:3.12-slim` is a moving target: the image
# that passed CI and the image that ships are otherwise different builds, and
# the difference is invisible in the Dockerfile.
FROM python:3.12-slim@sha256:af4e85f1cac90dd3771e47292ea7c8a9830abfabbe4faa5c53f158854c2e819d AS build

ENV PIP_NO_CACHE_DIR=1 PIP_DEFAULT_TIMEOUT=120 PIP_RETRIES=5
RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential libffi-dev \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md ./
COPY massingplan ./massingplan
# Into an isolated prefix, copied wholesale into the runtime stage below.
RUN pip install --prefix=/install ".[postgres,server]"


FROM python:3.12-slim@sha256:af4e85f1cac90dd3771e47292ea7c8a9830abfabbe4faa5c53f158854c2e819d

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MASSINGPLAN_ENV=production

# libpq only. No compiler, no headers.
RUN apt-get update && apt-get install -y --no-install-recommends \
        libpq5 curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=build /install /usr/local

WORKDIR /app
COPY massingplan ./massingplan
COPY migrations ./migrations
COPY alembic.ini wsgi.py ./
COPY deploy/docker-entrypoint.sh deploy/gunicorn.conf.py ./deploy/
RUN chmod +x deploy/docker-entrypoint.sh

# Non-root, and the writable directory is created and owned *before* any volume
# mounts. Docker seeds a named volume from the image path's contents and
# ownership, so /data ends up writable by appuser rather than root-only.
RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /data \
    && chown -R appuser:appuser /app /data
USER appuser
VOLUME ["/data"]

ENV MASSINGPLAN_DATABASE_URL=sqlite:////data/massingplan.db

EXPOSE 8000

# Liveness only. `/readyz` touches the database, and a liveness probe that fails
# on a brief database blip kills a container that would otherwise have recovered.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8000/healthz || exit 1

ENTRYPOINT ["./deploy/docker-entrypoint.sh"]
CMD ["gunicorn", "--config", "deploy/gunicorn.conf.py", "wsgi:app"]
