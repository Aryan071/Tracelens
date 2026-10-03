# Build context is the repo root (uv workspace + shared lockfile).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy PYTHONUNBUFFERED=1
COPY pyproject.toml uv.lock ./
COPY engine engine
COPY indexer indexer
RUN uv sync --frozen --package tracelens-indexer --no-dev
ENV PATH="/app/.venv/bin:$PATH"
# usage: docker run -v /path/to/repo:/repo tracelens-indexer index --repo /repo --scope src
ENTRYPOINT ["python", "-m", "tracelens_indexer"]
