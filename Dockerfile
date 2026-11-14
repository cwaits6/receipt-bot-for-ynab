# syntax=docker/dockerfile:1

FROM ghcr.io/astral-sh/uv:0.9-python3.12-bookworm-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project
COPY README.md LICENSE NOTICE ./
COPY src ./src
COPY scripts ./scripts
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

FROM python:3.12-slim-bookworm
RUN useradd --system --uid 10001 --no-create-home receiptbot \
    && mkdir /data && chown 10001:10001 /data
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
COPY --from=build /app/scripts /app/scripts
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    RECEIPTBOT_CONFIG=/config/config.yaml \
    RECEIPTBOT_DATA_DIR=/data
USER 10001
VOLUME ["/data"]
ENTRYPOINT ["receiptbot"]
