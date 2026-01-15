"""Authentication middleware for Reflect Collector.

This module provides Bearer token authentication for the Reflect ingestion
service. It follows the middleware pattern with clear lifecycle hooks.
"""

from __future__ import annotations

import os
import logging
import secrets
from typing import TYPE_CHECKING, Literal

from fastapi import Request, HTTPException
from starlette.middleware.base import BaseHTTPMiddleware

if TYPE_CHECKING:
    from starlette.responses import Response
    from typing import Callable, Awaitable

logger = logging.getLogger("reflect.auth")

# Configuration from environment
REFLECT_API_KEY = os.getenv("REFLECT_API_KEY", "")

# Endpoints exempt from authentication
PUBLIC_PATHS: frozenset[str] = frozenset({
    "/health",
    "/metrics", 
    "/docs",
    "/openapi.json",
    "/redoc",
})


class AuthenticationError(Exception):
    """Exception raised when authentication fails.
    
    This exception is raised when the configured exit behavior is `'error'`
    and authentication cannot be verified.
    
    Attributes:
        reason: Description of why authentication failed.
        path: The request path that failed authentication.
    """
    
    def __init__(
        self,
        reason: str,
        path: str,
    ) -> None:
        """Initialize the exception with failure details.
        
        Args:
            reason: Why authentication failed (missing token, invalid token, etc).
            path: The request path.
        """
        self.reason = reason
        self.path = path
        super().__init__(f"Authentication failed for {path}: {reason}")


def verify_token(token: str) -> bool:
    """Verify the provided Bearer token against the configured API key.
    
    Args:
        token: The token extracted from the Authorization header.
        
    Returns:
        True if the token is valid or if no API key is configured (dev mode).
        False if the token does not match.
    """
    if not REFLECT_API_KEY:
        # Dev mode: no key configured, allow all
        return True
    # Use constant-time comparison to prevent timing attacks
    return secrets.compare_digest(token.encode("utf-8"), REFLECT_API_KEY.encode("utf-8"))


class AuthMiddleware(BaseHTTPMiddleware):
    """Enforces Bearer token authentication on protected endpoints.
    
    This middleware monitors incoming requests and verifies the Authorization
    header contains a valid Bearer token. Public paths (health checks, docs)
    are exempt from authentication.
    
    Configuration:
        Set `REFLECT_API_KEY` environment variable to enable authentication.
        If not set, all requests are allowed (development mode).
    
    Example:
        ```python
        from fastapi import FastAPI
        from reflect.auth import AuthMiddleware
        
        app = FastAPI()
        app.add_middleware(AuthMiddleware)
        
        # Protected endpoint - requires Bearer token
        @app.post("/v1/traces")
        async def ingest(): ...
        
        # Public endpoint - no auth required
        @app.get("/health")
        def health(): ...
        ```
    
    Attributes:
        exit_behavior: What to do on auth failure ('error' raises, 'reject' returns 401).
    """
    
    def __init__(
        self,
        app,
        *,
        exit_behavior: Literal["error", "reject"] = "reject",
    ) -> None:
        """Initialize the authentication middleware.
        
        Args:
            app: The ASGI application.
            exit_behavior: What to do when authentication fails.
                - `'reject'`: Return HTTP 401 Unauthorized response.
                - `'error'`: Raise an `AuthenticationError` exception.
        """
        super().__init__(app)
        self.exit_behavior = exit_behavior
        
        if REFLECT_API_KEY:
            logger.info("Authentication enabled (REFLECT_API_KEY is set)")
        else:
            logger.warning("Authentication DISABLED - REFLECT_API_KEY not set")
    
    def _is_public_path(self, path: str) -> bool:
        """Check if the request path is exempt from authentication.
        
        Args:
            path: The URL path of the request.
            
        Returns:
            True if the path is in PUBLIC_PATHS, False otherwise.
        """
        return path in PUBLIC_PATHS
    
    def _extract_token(self, request: Request) -> str | None:
        """Extract Bearer token from Authorization header.
        
        Args:
            request: The incoming HTTP request.
            
        Returns:
            The token string if present and properly formatted, None otherwise.
        """
        auth_header = request.headers.get("Authorization", "")
        if auth_header.startswith("Bearer "):
            return auth_header[7:]  # Strip "Bearer " prefix
        return None
    
    async def dispatch(
        self,
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        """Process the request through the authentication check.
        
        Args:
            request: The incoming HTTP request.
            call_next: The next middleware or route handler.
            
        Returns:
            The response from downstream handlers if authenticated,
            or a 401 response if authentication fails.
            
        Raises:
            AuthenticationError: If exit_behavior is 'error' and auth fails.
        """
        path = request.url.path
        
        # Skip auth for public endpoints
        if self._is_public_path(path):
            return await call_next(request)
        
        # Dev mode: no key configured
        if not REFLECT_API_KEY:
            return await call_next(request)
        
        # Extract and verify token
        token = self._extract_token(request)
        
        if token is None:
            logger.warning(f"Missing Bearer token for {path}")
            if self.exit_behavior == "error":
                raise AuthenticationError("Missing Bearer token", path)
            raise HTTPException(status_code=401, detail="Missing Bearer token")
        
        if not verify_token(token):
            logger.warning(f"Invalid token for {path}")
            if self.exit_behavior == "error":
                raise AuthenticationError("Invalid API key", path)
            raise HTTPException(status_code=401, detail="Invalid API key")
        
        return await call_next(request)
