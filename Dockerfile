# Multi-stage build for alerting-service
#
# Uses unified-trading-services base from Artifact Registry.
# Cloud Build passes PROJECT_ID via --build-arg.

ARG PROJECT_ID
# Digest-pinned UTL base image (QG STEP 5.79 -- reproducible builds + UTL/UAC provenance).
# Refreshed by the dependency-update fan-out (update-dependency-version.yml) on base-image
# republish; cloudbuild may override at build time: --build-arg BASE_IMAGE_DIGEST=sha256:...
ARG BASE_IMAGE_DIGEST=sha256:7e3ddd4509df07aee54431c61b628cc553600d921d9ef9426a8800f764945a02
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

# Install this service (UTL + UAC pre-installed in the base image; --no-sources skips local path deps).
# uv does NOT read pip.conf's extra-index-url (pip-only convention) and its
# keyring-subprocess integration 401s against GAR in this container (unlike pip's
# in-process keyring import, which works) — see
# cloud_build_unified_api_contracts_publish_ordering_race_2026_07_29.md. Fix: mount a
# freshly-minted access token (same auth-precheck mechanism already proven against this
# exact index) as a BuildKit secret, scoped to only this RUN layer — never baked into an
# image layer or history.
# --upgrade-package unified-api-contracts (2026-08-07): the base image's pre-baked UAC copy
# already satisfies alerting-service's wide floor (>=0.95.0,<1.0.0), so a plain resolve leaves
# it untouched even when a newer UAC wheel with a real fix has since been published — this
# service's DP-alert routing rules live in UAC and need same-day freshness, unlike UTL. Scoped
# to this one package only (not a blanket --upgrade) to keep the rest of the base image's
# pinned resolution untouched. See
# alerting_service_lifecycle_events_sub_dual_consumer_slack_spam_2026_08_07.md.
# Retry-with-backoff (3 attempts, ~45s total budget): hardens against the exact
# publish-ordering-race window this doc tracks recurring on the next cross-repo floor-bump.
RUN --mount=type=secret,id=gar_token \
    UV_EXTRA_INDEX_URL="https://oauth2accesstoken:$(cat /run/secrets/gar_token)@asia-northeast1-python.pkg.dev/central-element-323112/unified-libraries/simple/" \
    sh -c 'i=1; until uv pip install --system --no-sources --upgrade-package unified-api-contracts -e .; do [ "$i" -ge 3 ] && { echo "uv pip install failed after 3 attempts" >&2; exit 1; }; w=$((15 * i)); echo "uv pip install failed (attempt $i/3) -- retrying in ${w}s"; sleep "$w"; i=$((i + 1)); done'

# Create non-root user; pre-create mock-mode cache dir needed by delivery_status tests
RUN addgroup --system appuser && adduser --system --ingroup appuser appuser
RUN mkdir -p /app/.local-dev-cache/alerting-service && chown -R appuser:appuser /app
USER appuser

# Reset base image ENTRYPOINT (base has ENTRYPOINT ["python"] which causes double-python invocation)
ENTRYPOINT []
EXPOSE 8080
CMD ["uvicorn", "alerting_service.api.main:app", "--host", "0.0.0.0", "--port", "8080"]
