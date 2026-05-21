# Multi-stage build for alerting-service
#
# Uses unified-trading-services base from Artifact Registry.
# Cloud Build passes PROJECT_ID via --build-arg.

ARG PROJECT_ID
FROM --platform=linux/amd64 asia-northeast1-docker.pkg.dev/${PROJECT_ID}/unified-trading-library/unified-trading-library:latest AS base

WORKDIR /app

# Stage 2: Application
FROM --platform=linux/amd64 base AS app

# Copy application code
COPY alerting_service/ /app/alerting_service/
COPY pyproject.toml uv.lock /app/
COPY README.md /app/

# Install dependencies from lockfile
RUN uv sync --frozen --no-dev --system

# Copy tests
COPY tests/ /app/tests/

# Copy scripts
COPY scripts/ /app/scripts/

# Run quality gates (tests inside image)
RUN bash scripts/quality-gates.sh --no-fix --quick

# Create non-root user
RUN addgroup --system appuser && adduser --system --ingroup appuser appuser
USER appuser

# Default command
CMD ["python", "-m", "alerting_service.cli.main", "--operation", "alerts", "--mode", "live"]
