# Alerting Service — Schema Validation

## Schema Location

- Alert schemas in service package or `schemas/`
- Pydantic models for alert payloads

## Validation Approach

- Alert payloads validated before persistence or publish
- Config schemas validated at load time

## Example

Pass: Valid alert with required fields. Fail: Missing timestamp or invalid severity.
