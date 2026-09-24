# One image definition for every service: docker build --build-arg SERVICE=voice-gateway .
# Build context is the repo root so the shared library is available.
FROM python:3.12-slim-bookworm AS build
ARG SERVICE
COPY --from=ghcr.io/astral-sh/uv:0.11.7 /uv /uvx /bin/
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
COPY libs ./libs
COPY services ./services
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --package "wassup-${SERVICE}"

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
# 30 s graceful shutdown so in-flight voice tool calls finish during a deploy.
CMD ["sh", "-c", "exec uvicorn $(echo ${SERVICE_MODULE} | tr - _).main:app --host 0.0.0.0 --port ${PORT:-8080} --timeout-graceful-shutdown 30 --no-server-header"]
