"""Alert routing rules for specific event categories."""

from .data_freshness_rules import route_data_freshness_event

__all__ = ["route_data_freshness_event"]
