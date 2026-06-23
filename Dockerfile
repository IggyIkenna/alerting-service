# Multi-stage build for alerting-service
#
# Uses unified-trading-services base from Artifact Registry.
# Cloud Build passes PROJECT_ID via --build-arg.

ARG PROJECT_ID
# Digest-pinned UTL base image (QG STEP 5.79 -- reproducible builds + UTL/UAC provenance).
# Refreshed by the dependency-update fan-out (update-dependency-version.yml) on base-image
# republish; cloudbuild may override at build time: --build-arg BASE_IMAGE_DIGEST=sha256:...
ARG BASE_IMAGE_DIGEST=sha256:3f2b47f29500ecc4a1c5005f616c5d8f6026afcf2d152167198ce46ae03e1a98
FROM --platform=linux/amd64 asia-northeast1-docker.pkg.dev/${PROJECT_ID}/unified-trading-library/unified-trading-library@${BASE_IMAGE_DIGEST} AS base

WORKDIR /app/alerting-service

# Stage 2: Application
FROM --platform=linux/amd64 base AS app

# Copy application code
COPY alerting_service/ ./alerting_service/
COPY pyproject.toml uv.lock README.md ./

# uv >= 0.11 removed --system from uv sync; UV_SYSTEM_PYTHON=1 is the cross-version equivalent.
ENV UV_SYSTEM_PYTHON=1
# Local path deps from uv.lock: ../unified-api-contracts → /app/unified-api-contracts (from WORKDIR /app/alerting-service)
COPY unified-api-contracts/ /app/unified-api-contracts/
COPY unified-trading-library/ /app/unified-trading-library/
RUN uv sync --frozen --no-dev
# uv sync creates .venv/ — add to PATH so uvicorn CMD resolves correctly
ENV PATH="/app/alerting-service/.venv/bin:${PATH}"

# Copy tests
COPY tests/ ./tests/

# Copy scripts
COPY scripts/ ./scripts/

# Copy cloudbuild for quality-gates manifest alignment check
COPY cloudbuild.yaml ./

# Create non-root user; pre-create mock-mode cache dir needed by delivery_status tests
RUN addgroup --system appuser && adduser --system --ingroup appuser appuser
RUN mkdir -p /app/.local-dev-cache/alerting-service && chown -R appuser:appuser /app
USER appuser

# Reset base image ENTRYPOINT (base has ENTRYPOINT ["python"] which causes double-python invocation)
ENTRYPOINT []
EXPOSE 8080
CMD ["uvicorn", "alerting_service.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
