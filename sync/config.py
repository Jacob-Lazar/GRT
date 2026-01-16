"""
Configuration for the Gate-to-Reflect Sync Daemon.

Uses Pydantic for validation and environment variable loading.
"""

import os
from typing import Optional
from pydantic import BaseModel, Field


class SyncConfig(BaseModel):
    """
    Configuration for the SyncDaemon.
    
    Attributes:
        reflect_endpoint: URL of the reflect collector's OTLP endpoint.
        sync_interval_sec: Interval between sync attempts in seconds.
        batch_size: Maximum spans per batch export.
        enabled: Whether the sync daemon is enabled.
        compression: Enable gzip compression for HTTP transport.
        timeout_ms: HTTP timeout in milliseconds.
    """
    reflect_endpoint: str = Field(
        default="http://localhost:8000/v1/traces",
        description="Reflect collector OTLP endpoint URL"
    )
    sync_interval_sec: float = Field(
        default=5.0,
        ge=0.1,
        le=300.0,
        description="Sync interval in seconds"
    )
    batch_size: int = Field(
        default=512,
        ge=1,
        le=10000,
        description="Maximum spans per batch"
    )
    enabled: bool = Field(
        default=True,
        description="Enable/disable sync daemon"
    )
    compression: bool = Field(
        default=True,
        description="Enable gzip compression"
    )
    timeout_ms: int = Field(
        default=30000,
        ge=1000,
        le=120000,
        description="HTTP timeout in milliseconds"
    )

    class Config:
        """Pydantic configuration."""
        frozen = True

    @classmethod
    def from_env(cls) -> "SyncConfig":
        """
        Load configuration from environment variables.
        
        Environment variables:
            GRT_SYNC_ENDPOINT: Reflect collector endpoint
            GRT_SYNC_INTERVAL_SEC: Sync interval in seconds
            GRT_SYNC_BATCH_SIZE: Batch size
            GRT_SYNC_ENABLED: Enable/disable (true/false)
            GRT_SYNC_COMPRESSION: Enable compression (true/false)
            GRT_SYNC_TIMEOUT_MS: HTTP timeout
        
        Returns:
            SyncConfig instance with values from environment
        """
        def _parse_bool(val: Optional[str], default: bool) -> bool:
            if val is None:
                return default
            return val.lower() in ("true", "1", "yes")
        
        return cls(
            reflect_endpoint=os.getenv(
                "GRT_SYNC_ENDPOINT",
                "http://localhost:8000/v1/traces"
            ),
            sync_interval_sec=float(os.getenv("GRT_SYNC_INTERVAL_SEC", "5.0")),
            batch_size=int(os.getenv("GRT_SYNC_BATCH_SIZE", "512")),
            enabled=_parse_bool(os.getenv("GRT_SYNC_ENABLED"), True),
            compression=_parse_bool(os.getenv("GRT_SYNC_COMPRESSION"), True),
            timeout_ms=int(os.getenv("GRT_SYNC_TIMEOUT_MS", "30000")),
        )
