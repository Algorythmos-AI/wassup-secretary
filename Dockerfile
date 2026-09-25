# One image definition for every service: docker build --build-arg SERVICE=voice-gateway .
# Build context is the repo root so the shared library is available.
FROM python:3.12-slim-bookworm AS build
ARG SERVICE
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/
# No BuildKit cache mount: Railway's builder only accepts cache mounts with a Railway-specific id,
# and the final image never carries the cache either way (only /app is copied into it).
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never UV_NO_CACHE=1
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
COPY db ./db
COPY deploy/entrypoint.sh ./deploy/entrypoint.sh
RUN uv sync --frozen --no-dev --package "wassup-${SERVICE}"

FROM python:3.12-slim-bookworm AS runtime
ARG SERVICE
ARG VERSION=0.0.0-dev
ARG GIT_TREE=unknown
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    WASSUP_VERSION=${VERSION} \
    WASSUP_GIT_TREE=${GIT_TREE} \
    SERVICE_MODULE=${SERVICE}
RUN useradd --system --uid 10001 --home /app app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
USER app
EXPOSE 8080
# Role (serve / bootstrap) and optional migrate-on-start come from the environment: see the script.
CMD ["/app/deploy/entrypoint.sh"]
