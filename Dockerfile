# Production Dockerfile for PreFlight QC
# Multi-agent media delivery QC compliance & redelivery prediction

FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080 \
    APP_ENV=production

WORKDIR /app

# Install system utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install uv for fast, reproducible dependency installation
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Copy dependency definition
COPY pyproject.toml uv.lock* LICENSE ./

# Install Python runtime dependencies into /app/.venv
ENV PATH="/app/.venv/bin:$PATH"
RUN uv sync --no-dev --frozen

# Copy application source
COPY agents/ ./agents/
COPY data/ ./data/
COPY mcp_config/ ./mcp_config/
COPY web/ ./web/

# Expose web server port
EXPOSE 8080

# Run PreFlight QC web dashboard
CMD ["python", "web/server.py"]
