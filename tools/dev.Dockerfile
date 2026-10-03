# Tooling image for make targets: uv + Python 3.12 + git (the indexer shells out to git).
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
