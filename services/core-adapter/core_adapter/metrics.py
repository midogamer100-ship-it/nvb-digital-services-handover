"""Prometheus metrics. Labels are bounded (endpoint names / outcome classes, never customer ids)."""

from prometheus_client import Counter, Gauge, Histogram

CORE_REQUESTS = Counter("adapter_core_requests_total", "Calls from the adapter to the core", ["endpoint", "outcome"])
CORE_DURATION = Histogram("adapter_core_request_seconds", "Core call latency", ["endpoint"],
                          buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10))
CORE_INFLIGHT = Gauge("adapter_core_inflight", "Core calls in flight on this replica")
CORE_REJECTED = Counter("adapter_core_rejected_total", "Calls rejected locally to protect the core", ["reason"])
API_REQUESTS = Counter("adapter_api_requests_total", "Adapter API requests", ["route", "status_class"])
