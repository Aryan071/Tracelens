# Build context is the repo root (uv workspace + shared lockfile).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
COPY engine engine
COPY indexer indexer
COPY api api
RUN uv sync --frozen --package tracelens-api --no-dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 8080
CMD ["python", "-m", "tracelens_api.main"]
