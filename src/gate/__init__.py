"""
gate
~~~~

Lightweight Observability SDK for AI Agents.
"""

from .api import *
from ._compat import (
    JSON_ENCODER, PROTOBUF_AVAILABLE, HTTP_CLIENT, json_dumps, json_loads
)

__version__ = "1.0.0"
__all__ = [
    # Configuration
    "configure", "get_config", "ProbeConfig",

    # Tracing
    "get_tracer", "shutdown", "force_flush", "get_current_span_context",

    # Decorators
    "observe", "trace_span",

    # Micro-Instrumentation
    "step", "track", "atrack", "event", "current_span",

    # Agent-Native
    "thought", "ThoughtContext",
    "guard", "Guard", "CostBudgetExceeded", "LoopBudgetExceeded",
    "snapshot", "branch", "BranchContext",

    # Propagation
    "inject_context", "extract_context", "get_injection_headers",
    "trace_from_headers", "instrument_http_clients", "get_grt_tracestate",

    # Models
    "Span", "SpanKind", "StatusCode", "SpanContext",

    # Instrumentation
    "install_import_hook", "patch_existing_base_agent", "instrument_all",

    # Diagnostics
    "get_json_encoder",
]
