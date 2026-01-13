"""
Prometheus Metrics for Reflect Collector.
"""
import time
import logging
from functools import wraps
from typing import Callable
from fastapi import Request, Response
from starlette.middleware.base import BaseHTTPMiddleware
from prometheus_client import Counter, Histogram, generate_latest, CONTENT_TYPE_LATEST

logger = logging.getLogger("reflect.metrics")

# --- Counters ---
SPANS_INGESTED = Counter(
    "reflect_spans_ingested_total",
    "Total number of spans ingested",
    ["endpoint"]
)

REQUESTS_TOTAL = Counter(
    "reflect_requests_total",
    "Total HTTP requests",
    ["method", "path", "status"]
)

# --- Histograms ---
REQUEST_LATENCY = Histogram(
    "reflect_request_latency_seconds",
    "Request latency in seconds",
    ["method", "path"],
    buckets=[0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0]
)

DB_WRITE_LATENCY = Histogram(
    "reflect_db_write_latency_seconds",
    "Database write latency in seconds",
    buckets=[0.001, 0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1.0]
)


class MetricsMiddleware(BaseHTTPMiddleware):
    """Middleware to record request metrics."""
    
    async def dispatch(self, request: Request, call_next: Callable):
        method = request.method
        path = request.url.path
        
        # Skip metrics endpoint itself to avoid recursion
        if path == "/metrics":
            return await call_next(request)
        
        start_time = time.perf_counter()
        response = await call_next(request)
        duration = time.perf_counter() - start_time
        
        # Record metrics
        REQUESTS_TOTAL.labels(
            method=method,
            path=path,
            status=response.status_code
        ).inc()
        
        REQUEST_LATENCY.labels(
            method=method,
            path=path
        ).observe(duration)
        
        return response


def record_spans_ingested(count: int, endpoint: str = "otlp"):
    """Record the number of spans ingested."""
    SPANS_INGESTED.labels(endpoint=endpoint).inc(count)


def record_db_write(duration: float):
    """Record database write latency."""
    DB_WRITE_LATENCY.observe(duration)


async def metrics_endpoint():
    """Generate Prometheus metrics output."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST
    )
