# Testing

## Run Tests

```bash
pytest tests/ -v
```

## Coverage Target

Target: 70%+ coverage. Run `pytest tests/ --cov --cov-report=term-missing`.

## Known Exclusions

- Integration tests requiring live GCP/APIs are marked with `@pytest.mark.integration` and skipped without credentials.

