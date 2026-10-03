# Build context is the repo root (uv workspace + shared lockfile).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
COPY engine engine
COPY ingest ingest
RUN uv sync --frozen --package tracelens-ingest --no-dev
ENV PATH="/app/.venv/bin:$PATH"
EXPOSE 4317 4318 9464
CMD ["python", "-m", "tracelens_ingest.main"]
