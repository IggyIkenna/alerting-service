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

# uv >= 0.11 removed --system from uv sync; UV_SYSTEM_PYTHON=1 is the cross-version equivalent.
ENV UV_SYSTEM_PYTHON=1
# Local path deps from uv.lock: ../unified-api-contracts → /unified-api-contracts (from WORKDIR /app)
COPY unified-api-contracts/ /unified-api-contracts/
COPY unified-trading-library/ /unified-trading-library/
RUN uv sync --frozen --no-dev

# Copy tests
COPY tests/ /app/tests/

# Copy scripts
COPY scripts/ /app/scripts/

# Create non-root user
RUN addgroup --system appuser && adduser --system --ingroup appuser appuser
USER appuser

# Reset base image ENTRYPOINT (base has ENTRYPOINT ["python"] which causes double-python invocation)
ENTRYPOINT []
EXPOSE 8080
CMD ["uvicorn", "alerting_service.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
