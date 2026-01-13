"""W3C Baggage context extraction for user/session tracking.

This module extracts context from the W3C Baggage header and injects
allowed keys into span attributes. This enables filtering traces by
user_id, session_id, or tenant_id in the dashboard.

Specification: https://www.w3.org/TR/baggage/
"""

from __future__ import annotations

import logging
import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from fastapi import Request

logger = logging.getLogger("reflect.baggage")

# Keys allowed to be extracted and stored (cardinality protection)
ALLOWED_KEYS: frozenset[str] = frozenset({
    "user_id",
    "session_id",
    "tenant_id",
    "environment",
    "plan",
    "agent_id",
})

# Keys explicitly blocked (common mistakes that cause cardinality explosion)
BLOCKED_KEYS: frozenset[str] = frozenset({
    "request_id",
    "trace_id",
    "correlation_id",
    "timestamp",
})

# Constraints
MAX_KEY_LENGTH = 64
MAX_VALUE_LENGTH = 256
MAX_KEYS_PER_REQUEST = 10


class BaggageParseError(Exception):
    """Exception raised when baggage header parsing fails."""
    pass


def parse_baggage_header(header_value: str) -> dict[str, str]:
    """Parse W3C Baggage header into key-value pairs.
    
    Format: `key1=value1,key2=value2`
    
    Values may be URL-encoded. Properties (`;key=value`) are ignored.
    
    Args:
        header_value: The raw Baggage header string.
        
    Returns:
        Dictionary of key-value pairs from the header.
        
    Example:
        ```python
        >>> parse_baggage_header("user_id=alice,session_id=123")
        {"user_id": "alice", "session_id": "123"}
        ```
    """
    result = {}
    
    if not header_value:
        return result
    
    # Split by comma (member separator)
    members = header_value.split(",")
    
    for member in members[:MAX_KEYS_PER_REQUEST]:
        member = member.strip()
        if not member:
            continue
        
        # Split by semicolon to separate key=value from properties
        parts = member.split(";")
        kv_part = parts[0].strip()
        
        if "=" not in kv_part:
            continue
        
        key, value = kv_part.split("=", 1)
        key = key.strip()
        value = value.strip()
        
        # Validate key length
        if len(key) > MAX_KEY_LENGTH:
            logger.warning(f"Baggage key too long, skipping: {key[:20]}...")
            continue
        
        # Truncate value if too long
        if len(value) > MAX_VALUE_LENGTH:
            value = value[:MAX_VALUE_LENGTH]
            logger.debug(f"Baggage value truncated: {key}")
        
        # URL decode if needed
        try:
            from urllib.parse import unquote
            value = unquote(value)
        except Exception:
            pass
        
        result[key] = value
    
    return result


def filter_baggage(baggage: dict[str, str]) -> dict[str, str]:
    """Filter baggage to only allowed keys.
    
    Removes blocked keys and keys not in the allowlist to prevent
    cardinality explosion in the analytics database.
    
    Args:
        baggage: Raw baggage key-value pairs.
        
    Returns:
        Filtered baggage with only allowed keys.
    """
    filtered = {}
    
    for key, value in baggage.items():
        if key in BLOCKED_KEYS:
            logger.debug(f"Blocked baggage key rejected: {key}")
            continue
        
        if key not in ALLOWED_KEYS:
            logger.debug(f"Unknown baggage key ignored: {key}")
            continue
        
        filtered[key] = value
    
    return filtered


def extract_baggage(request: Request) -> dict[str, str]:
    """Extract and filter baggage from a FastAPI request.
    
    This is the main entry point for baggage extraction in the Reflect
    collector. It parses the Baggage header and applies the allowlist.
    
    Args:
        request: The incoming FastAPI request.
        
    Returns:
        Filtered baggage dictionary ready for span injection.
        
    Example:
        ```python
        @app.post("/v1/traces")
        async def ingest(request: Request, payload: TracePayload):
            baggage = extract_baggage(request)
            # baggage = {"user_id": "alice", "session_id": "123"}
        ```
    """
    header_value = request.headers.get("baggage", "")
    
    if not header_value:
        return {}
    
    try:
        raw_baggage = parse_baggage_header(header_value)
        filtered = filter_baggage(raw_baggage)
        
        if filtered:
            logger.debug(f"Extracted baggage: {list(filtered.keys())}")
        
        return filtered
    
    except Exception as e:
        logger.warning(f"Failed to parse baggage header: {e}")
        return {}


def inject_baggage_into_spans(
    spans: list[dict],
    baggage: dict[str, str],
) -> list[dict]:
    """Inject baggage context into span attributes.
    
    Adds baggage keys to each span's attributes_json field with a
    `baggage.` prefix to avoid collisions.
    
    Args:
        spans: List of flattened span dictionaries.
        baggage: Filtered baggage key-value pairs.
        
    Returns:
        Spans with baggage injected into attributes.
    """
    if not baggage:
        return spans
    
    for span in spans:
        # Parse existing attributes
        attrs_str = span.get("attributes_json", "{}")
        try:
            # Handle both dict and str formats
            if isinstance(attrs_str, str):
                # Simple eval for our str(dict) format - not ideal but matches current impl
                attrs = eval(attrs_str) if attrs_str else {}
            else:
                attrs = attrs_str
        except Exception:
            attrs = {}
        
        # Inject baggage with prefix
        for key, value in baggage.items():
            attrs[f"baggage.{key}"] = value
        
        span["attributes_json"] = str(attrs)
    
    return spans
