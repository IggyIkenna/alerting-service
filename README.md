# alerting-system

Multi-channel alerting (Slack, email, PagerDuty) for system health and trading events

## Type
platform

## Tech Stack
Python

## Mode Support
- Batch: ❌
- Live: ✅

## Upstream Dependencies
- unified-cloud-services

## Deployment
cloud_run_services

## Priority
P1-high

## Owner
Harsh

---

## Development

### Setup

```bash
# For Python services
uv pip install -e ".[dev]"

# Install pre-commit hooks
pre-commit install

# For UI services
npm install
```

### Pre-commit Hooks

Install and run pre-commit hooks:

```bash
# Install hooks (one-time setup)
pre-commit install

# Run hooks on all files
pre-commit run --all-files

# Hooks run automatically on commit
git commit -m "your message"
```

### Quality Gates

```bash
bash scripts/quality-gates.sh        # Auto-fix
bash scripts/quality-gates.sh --no-fix  # Verify
```

### Quickmerge

```bash
bash scripts/quickmerge.sh "commit message"
```

## Codex Reference

See unified-trading-codex for all standards and patterns.
