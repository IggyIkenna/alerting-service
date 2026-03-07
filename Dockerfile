# Multi-stage build for alerting-system
#
# Uses unified-trading-services base from Artifact Registry.
# Cloud Build passes PROJECT_ID via --build-arg.

ARG PROJECT_ID
FROM asia-northeast1-docker.pkg.dev/${PROJECT_ID}/unified-trading-library/unified-trading-library:latest AS base

WORKDIR /app

# Stage 2: Application
FROM base AS app

# Copy application code
COPY alerting_system/ /app/alerting_system/
COPY pyproject.toml /app/
COPY README.md /app/

# Install service with dev dependencies (for tests)
RUN uv pip install --system -e ".[dev]"

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
CMD ["python", "-m", "alerting_service.main", "--mode", "batch"]
