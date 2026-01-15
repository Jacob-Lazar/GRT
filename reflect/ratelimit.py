"""Rate limiting middleware for Reflect Collector.

This module provides token-bucket rate limiting to protect the ingestion
service from denial-of-service attacks and runaway agents.

Configuration via environment variables:
    REFLECT_RATE_LIMIT_ENABLED: Enable rate limiting (default: true)
    REFLECT_RATE_LIMIT_REQUESTS: Max requests per window (default: 100)
    REFLECT_RATE_LIMIT_WINDOW_SECONDS: Window size in seconds (default: 60)
"""

from __future__ import annotations

import logging
import os
import time
from collections import defaultdict
from typing import TYPE_CHECKING, Callable, Awaitable

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import Response

if TYPE_CHECKING:
    pass

logger = logging.getLogger("reflect.ratelimit")

# Configuration from environment
RATE_LIMIT_ENABLED = os.getenv("REFLECT_RATE_LIMIT_ENABLED", "true").lower() == "true"
RATE_LIMIT_REQUESTS = int(os.getenv("REFLECT_RATE_LIMIT_REQUESTS", "100"))
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("REFLECT_RATE_LIMIT_WINDOW_SECONDS", "60"))

# Paths exempt from rate limiting
EXEMPT_PATHS: frozenset[str] = frozenset({
    "/health",
    "/metrics",
    "/docs",
    "/openapi.json",
    "/redoc",
})


class RateLimitExceeded(Exception):
    """Exception raised when rate limit is exceeded.
    
    Attributes:
        client_id: Identifier of the rate-limited client.
        retry_after: Seconds until the client can retry.
    """
    
    def __init__(self, client_id: str, retry_after: int) -> None:
        self.client_id = client_id
        self.retry_after = retry_after
        super().__init__(f"Rate limit exceeded for {client_id}. Retry after {retry_after}s")


class TokenBucket:
    """Simple in-memory token bucket for rate limiting.
    
    Each client gets a bucket that refills at a constant rate.
    Thread-safe for async use within a single process.
    
    Note: For distributed deployments, replace with Redis-based limiter.
    """
    
    def __init__(
        self,
        max_tokens: int = RATE_LIMIT_REQUESTS,
        refill_seconds: int = RATE_LIMIT_WINDOW_SECONDS,
    ) -> None:
        """Initialize the token bucket.
        
        Args:
            max_tokens: Maximum tokens (requests) per bucket.
            refill_seconds: Time window for full refill.
        """
        self.max_tokens = max_tokens
        self.refill_rate = max_tokens / refill_seconds
        self._buckets: dict[str, tuple[float, float]] = defaultdict(
            lambda: (float(max_tokens), time.monotonic())
        )
    
    def _refill(self, client_id: str) -> float:
        """Refill bucket based on elapsed time."""
        tokens, last_update = self._buckets[client_id]
        now = time.monotonic()
        elapsed = now - last_update
        new_tokens = min(self.max_tokens, tokens + elapsed * self.refill_rate)
        self._buckets[client_id] = (new_tokens, now)
        return new_tokens
    
    def consume(self, client_id: str, tokens: int = 1) -> tuple[bool, int]:
        """Try to consume tokens from the bucket.
        
        Args:
            client_id: Unique client identifier.
            tokens: Number of tokens to consume.
            
        Returns:
            Tuple of (allowed, retry_after_seconds).
        """
        current_tokens = self._refill(client_id)
        
        if current_tokens >= tokens:
            self._buckets[client_id] = (
                current_tokens - tokens,
                time.monotonic()
            )
            return True, 0
        
        # Calculate retry-after
        tokens_needed = tokens - current_tokens
        retry_after = int(tokens_needed / self.refill_rate) + 1
        return False, retry_after


# Global bucket instance
_bucket = TokenBucket()


class RateLimitMiddleware(BaseHTTPMiddleware):
    """Middleware that enforces request rate limits.
    
    Uses client IP as the rate limit key. Protected endpoints return
    HTTP 429 when the limit is exceeded.
    
    Example:
        ```python
        from fastapi import FastAPI
        from reflect.ratelimit import RateLimitMiddleware
        
        app = FastAPI()
        app.add_middleware(RateLimitMiddleware)
        ```
    """
    
    def _get_client_id(self, request: Request) -> str:
        """Extract client identifier from request.
        
        Uses X-Forwarded-For if behind a proxy, otherwise client host.
        """
        forwarded = request.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return request.client.host if request.client else "unknown"
    
    def _is_exempt(self, path: str) -> bool:
        """Check if path is exempt from rate limiting."""
        return path in EXEMPT_PATHS
    
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Process the request through rate limit check.
        
        Args:
            request: The incoming HTTP request.
            call_next: The next middleware or route handler.
            
        Returns:
            The response if allowed, or 429 Too Many Requests.
        """
        # Skip if disabled
        if not RATE_LIMIT_ENABLED:
            return await call_next(request)
        
        # Skip exempt paths
        path = request.url.path
        if self._is_exempt(path):
            return await call_next(request)
        
        # Check rate limit
        client_id = self._get_client_id(request)
        allowed, retry_after = _bucket.consume(client_id)
        
        if not allowed:
            logger.warning(f"Rate limit exceeded for {client_id} on {path}")
            raise HTTPException(
                status_code=429,
                detail="Too many requests",
                headers={"Retry-After": str(retry_after)},
            )
        
        return await call_next(request)
