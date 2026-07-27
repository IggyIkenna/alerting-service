# Multi-stage build for alerting-service
#
# Uses unified-trading-services base from Artifact Registry.
# Cloud Build passes PROJECT_ID via --build-arg.

ARG PROJECT_ID
# Digest-pinned UTL base image (QG STEP 5.79 -- reproducible builds + UTL/UAC provenance).
# Refreshed by the dependency-update fan-out (update-dependency-version.yml) on base-image
# republish; cloudbuild may override at build time: --build-arg BASE_IMAGE_DIGEST=sha256:...
ARG BASE_IMAGE_DIGEST=sha256:1390ea307339a30ece70ca0713bb3b42e56ed2a68a51aac345b6b009127f8e7f
FROM --platform=linux/amd64 asia-northeast1-docker.pkg.dev/${PROJECT_ID}/unified-trading-library/unified-trading-library@${BASE_IMAGE_DIGEST} AS base

WORKDIR /app/alerting-service

# Stage 2: Application
FROM --platform=linux/amd64 base AS app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Install uv (bootstrap with pip — acceptable QG exception, bootstraps uv before uv is available)
RUN pip install --no-cache-dir uv  # uv bootstrap
# keyring FIRST (before pip.conf) so Artifact Registry auth works without an auth loop
RUN uv pip install --system --no-cache-dir keyrings.google-artifactregistry-auth
# NOW copy pip.conf — keyring is ready to resolve the unified-libraries AR index
COPY pip.conf /etc/pip.conf

# Copy the WHOLE single-repo build context (tests/scripts/cloudbuild needed by the QG step).
# Sibling source repos are NOT in Cloud Build's context — UTL+UAC are PRE-INSTALLED in the base
# image, and --no-sources below ignores the [tool.uv.sources] local path deps and resolves any
# version bumps from the AR index instead of COPYing ../unified-* (which fails in single-repo CI).
COPY . .

# hatch-vcs (source = "vcs"): .git is .dockerignore'd + COPY . . excludes it, so `uv pip install -e .`
# cannot run `git describe`. Cloud Build resolves the real tag in extract-version and passes it via
# --build-arg SETUPTOOLS_SCM_PRETEND_VERSION; export it BEFORE the install else setuptools-scm fails
# with "unable to detect version for /workspace". Default keeps a local `docker build` working.
ARG SETUPTOOLS_SCM_PRETEND_VERSION=0.0.0.dev0
ENV SETUPTOOLS_SCM_PRETEND_VERSION=${SETUPTOOLS_SCM_PRETEND_VERSION}

# Install this service (UTL + UAC pre-installed in the base image; --no-sources skips local path deps)
RUN uv pip install --system --no-sources -e .

# Create non-root user; pre-create mock-mode cache dir needed by delivery_status tests
RUN addgroup --system appuser && adduser --system --ingroup appuser appuser
RUN mkdir -p /app/.local-dev-cache/alerting-service && chown -R appuser:appuser /app
USER appuser

# Reset base image ENTRYPOINT (base has ENTRYPOINT ["python"] which causes double-python invocation)
ENTRYPOINT []
EXPOSE 8080
CMD ["uvicorn", "alerting_service.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
