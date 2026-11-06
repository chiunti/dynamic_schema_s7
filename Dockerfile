FROM python:3.14-slim AS base

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1

# Set work directory
WORKDIR /app

# Create non-root user (shared by all targets)
RUN useradd -m appuser


# mcp — s7-author/s7-catalog MCP servers (stdio + streamable-http). Dedicated
# dependency env: the package is standalone (no Django imports); sharing the
# app env lets semgrep's transitive `mcp==1.29.0` cap the SDK pin >=1.19,<3.
FROM base AS mcp

# Install Python dependencies — the s7_mcp pins resolve alone here.
COPY s7_mcp/requirements.txt /app/s7_mcp/requirements.txt
RUN pip install --upgrade pip && \
    pip install -r s7_mcp/requirements.txt

# Copy project
COPY . /app/
RUN chown -R appuser:appuser /app
USER appuser

EXPOSE 8000

CMD ["python", "-m", "s7_mcp"]


# web — Django app + editor-extension scanner. s7_mcp requirements resolve in
# the same pip call so `mcp` pins never conflict with transitive dependents
# (semgrep declares the exact `mcp==1.29.0`).
FROM base AS web

# Install system dependencies
RUN apt-get update && apt-get install -y \
    gcc \
    libpq-dev \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt /app/
COPY s7_mcp/requirements.txt /app/s7_mcp/requirements.txt
RUN pip install --upgrade pip && \
    pip install -r requirements.txt -r s7_mcp/requirements.txt

# Copy project
COPY . /app/
RUN chown -R appuser:appuser /app
USER appuser

# Expose port
EXPOSE 8000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8000/health/')" || exit 1

# Default command - use gunicorn for production
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "4", "--threads", "2", "config.wsgi:application"]
