# Multi-stage build for alerting-system
#
# Uses unified-cloud-services base from Artifact Registry.
# Cloud Build passes PROJECT_ID via --build-arg.

ARG PROJECT_ID
FROM asia-northeast1-docker.pkg.dev/${PROJECT_ID}/unified-cloud-services/unified-cloud-services:latest AS base

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

# Default command
CMD ["python", "-m", "alerting_system.main"]
