"""Prometheus metrics for alerting-service."""

from prometheus_client import Counter, Gauge, Histogram

RECORDS_PROCESSED = Counter(
    "alerting_service_records_processed_total",
    "Total number of alert events processed",
    ["status"],  # labels: success / error
)

PROCESSING_LATENCY = Histogram(
    "alerting_service_processing_latency_seconds",
    "Alert event processing latency in seconds",
    buckets=[0.1, 0.5, 1.0, 2.5, 5.0, 10.0],
)

SERVICE_ERRORS_TOTAL = Counter(
    "alerting_service_service_errors_total",
    "Total SERVICE_ERROR events received",
    ["service", "venue"],
)

CIRCUIT_STATE_TRANSITIONS = Counter(
    "alerting_service_circuit_state_transitions_total",
    "Circuit breaker state transitions",
    ["service", "venue", "new_state"],
)

CIRCUITS_OPEN = Gauge(
    "alerting_service_circuits_open",
    "Number of circuits currently in OPEN state",
)
