"""Prometheus metrics for alerting-service."""

from prometheus_client import Counter, Histogram

# UAC contract type references — canonical types for cross-service contract alignment
from unified_api_contracts import (
    LatencyBenchmarkReport,  # noqa: F401
    LatencyPercentile,  # noqa: F401
    NetworkJitterMetric,  # noqa: F401
    OrderLatencyRecord,  # noqa: F401
    SubMillisecondLatencyRecord,  # noqa: F401
    TickToTradeMetric,  # noqa: F401
)

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
