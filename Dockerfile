# Dockerfile for Glama automated evaluation & containerized MCP execution
FROM python:3.12-slim

WORKDIR /app

# Install minimal system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    git \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast, reliable dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# Copy project specification & lockfiles
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/

# Install dependencies into virtual environment
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONUNBUFFERED=1

# Expose stdio entrypoint for MCP clients and Glama introspection
ENTRYPOINT ["vps-mcp", "serve"]
