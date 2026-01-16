"""
gate.langgraph_instrumentor
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Public API for LangGraph auto-instrumentation.

This module provides the public interface for LangGraph instrumentation.
The actual implementation resides in _internal/langgraph.py.

Usage:
    >>> from gate.langgraph_instrumentor import instrument_langgraph
    >>> result = instrument_langgraph()
    >>> print(f"Instrumentation enabled: {result}")
"""

from __future__ import annotations

from ._internal.langgraph import instrument_langgraph

__all__ = ["instrument_langgraph"]
