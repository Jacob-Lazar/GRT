"""
gate.api
~~~~~~~~

This module implements the Gate public API.

Design Principles:
    1. Agent's main thread must NEVER block for telemetry
    2. Memory allocated once at init (zero GC during runtime)
    3. Fail-open: telemetry failures don't affect agent
    4. Zero code changes with import hook patching
"""

import os
from typing import Any

from ._compat import JSON_ENCODER, PROTOBUF_AVAILABLE, HTTP_CLIENT
from .config import configure, get_config, ProbeConfig

# Store capabilities for runtime inspection
_CAPABILITIES = {
    "json_backend": JSON_ENCODER,
    "protobuf_available": PROTOBUF_AVAILABLE,
    "http_backend": HTTP_CLIENT,
}

def get_capabilities() -> dict[str, Any]:
    """
    Get the current runtime capabilities of gate.
    
    Returns:
        Capability flags showing which optional dependencies are active
        
    !!! example
        ```python
        caps = get_capabilities()
        if caps['json_backend'] == 'stdlib':
            print("Consider installing orjson for better performance")
        ```
    """
    return _CAPABILITIES.copy()


# Optional: Print capabilities on first import (controlled by env var)
if os.getenv("GRT_VERBOSE") == "1":
    print(f"[gate] Capabilities:")
    print(f"  JSON: {JSON_ENCODER} {'✓' if JSON_ENCODER == 'orjson' else '(slower, stdlib)'}")
    print(f"  Protobuf: {'✓' if PROTOBUF_AVAILABLE else '✗ (using JSON)'}")
    print(f"  HTTP: {HTTP_CLIENT} {'✓' if HTTP_CLIENT == 'requests' else '(stdlib urllib)'}")

from .core import (
    get_tracer, shutdown, force_flush, get_current_span_context,
    # High-performance decorators and helpers
    observe, trace_span,
    # Micro-instrumentation
    step, track, atrack, event, current_span,
    # UNIQUE: Agent-Native Observability (not in Langfuse/Datadog/New Relic)
    thought, ThoughtContext,           # Chain-of-Thought capture
    guard, Guard, CostBudgetExceeded, LoopBudgetExceeded,  # Cost/loop budgets
    snapshot,                          # State capture
    branch, BranchContext,             # Decision tree tracking
)
from .models import Span, SpanKind, StatusCode, SpanContext
from .integrations import install_import_hook, patch_existing_base_agent
from .hooks import instrument_all
from .utils import (
    inject_context,
    extract_context,
    get_injection_headers,
    trace_from_headers,
    instrument_http_clients,
    get_grt_tracestate,
)
from .transport import get_json_encoder
