"""Prometheus metrics for alerting-service."""

from prometheus_client import Counter, Histogram

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
