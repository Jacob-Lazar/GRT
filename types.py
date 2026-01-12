"""
gate.types
~~~~~~~~~~

Type aliases and protocols for the Gate SDK.

This module defines reusable type aliases following LangChain conventions.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any, TypeVar

if TYPE_CHECKING:
    from collections.abc import Callable

# =============================================================================
# Type Aliases
# =============================================================================

AttributeValue = str | int | float | bool | list[str] | list[int] | list[float]
"""Valid types for span attribute values."""

TraceId = str
"""32-character hex string identifying a trace."""

SpanId = str
"""16-character hex string identifying a span."""

SpanName = str
"""Human-readable span name (e.g., 'agent.execute')."""

Headers = dict[str, str]
"""HTTP headers dictionary."""

Attributes = dict[str, Any]
"""Span attributes dictionary."""

# =============================================================================
# Generic Type Variables
# =============================================================================

T = TypeVar("T")
"""Generic type variable."""

F = TypeVar("F", bound="Callable[..., Any]")
"""Type variable for callable decorators."""

# =============================================================================
# Exports
# =============================================================================

__all__ = [
    "AttributeValue",
    "TraceId",
    "SpanId",
    "SpanName",
    "Headers",
    "Attributes",
    "T",
    "F",
]
