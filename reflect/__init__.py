"""FL-GRT Reflect Collector Package.

The Reflect collector is the central ingestion point for all agent telemetry.
It accepts OpenTelemetry (OTLP) trace data and simplified webhook payloads
from low-code platforms.
"""

__version__ = "1.1.0"
__all__ = [
    "main",
    "auth",
    "models",
    "storage",
    "queue",
    "metrics",
    "baggage",
    "webhook",
    "ratelimit",
]
