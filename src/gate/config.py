"""
Configuration module for gate.

This module handles all configuration for the observability SDK,
supporting both environment variables and programmatic configuration.

Configuration Priority (highest to lowest):
    1. Programmatic configuration via configure()
    2. Environment variables
    3. Default values

Environment Variables:
    GATE_ENABLED: Enable/disable instrumentation (default: true)
    GATE_ENDPOINT: OTLP collector endpoint (default: http://localhost:4318)
    GATE_SERVICE_NAME: Service name for traces (default: unknown-service)
    GATE_ENVIRONMENT: Environment tag (default: development)
    GATE_SAMPLE_RATE: Sampling rate 0.0-1.0 (default: 1.0)
    GATE_BATCH_SIZE: Spans per export batch (default: 512)
    GATE_BUFFER_CAPACITY: Ring buffer capacity (default: 4096)
    GATE_EXPORT_INTERVAL_MS: Export interval in ms (default: 5000)
    GATE_EXPORT_TIMEOUT_MS: Export timeout in ms (default: 30000)
    GATE_LOG_LEVEL: Logging verbosity (default: WARNING)
"""

from __future__ import annotations

import os
import logging
from dataclasses import dataclass, field
from typing import Any
from enum import Enum

__all__ = [
    "LogLevel",
    "ProbeConfig",
    "get_config",
    "configure",
    "lock_config",
    "reset_config",
]

class LogLevel(str, Enum):
    """Log level enum matching Python logging levels."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


def _get_bool_env(key: str, default: bool) -> bool:
    """Parse boolean from environment variable."""
    value = os.getenv(key)
    if value is None:
        return default
    return value.lower() in ('true', '1', 'yes', 'on')


def _get_float_env(key: str, default: float) -> float:
    """Parse float from environment variable."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default


def _get_int_env(key: str, default: int) -> int:
    """Parse int from environment variable."""
    value = os.getenv(key)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError:
        return default


@dataclass
class ProbeConfig:
    """
    Configuration for gate SDK.
    
    All settings can be overridden via environment variables or programmatically.
    
    Attributes:
        enabled: Master switch for instrumentation.
        endpoint: OTLP collector endpoint (HTTP).
        service_name: Service name attached to all spans.
        service_version: Service version (optional).
        environment: Environment tag (development, staging, production).
        sample_rate: Probability of sampling a trace (0.0 to 1.0).
        batch_size: Number of spans per export batch.
        buffer_capacity: Ring buffer capacity (power of 2).
        export_interval_ms: Milliseconds between export attempts.
        export_timeout_ms: Export HTTP timeout in milliseconds.
        compression: Enable gzip compression (reduces bandwidth ~70%).
        log_level: SDK internal logging level.
        resource_attributes: Additional resource attributes.
        headers: HTTP headers for authentication.

    Examples:
        !!! example "Configure via environment"
            ```bash
            export GATE_SERVICE_NAME="my-agent"
            export GATE_ENDPOINT="http://collector:4318/v1/traces"
            ```
            
        !!! example "Configure programmatically"
            ```python
            from gate import configure
            
            configure(
                service_name="payment-service",
                environment="production",
                sample_rate=0.1
            )
            ```
    """
    
    # Core settings
    enabled: bool = True
    endpoint: str = "http://localhost:4318/v1/traces"
    
    # Service identification
    service_name: str = "unknown-service"
    service_version: str = "0.0.0"
    environment: str = "development"
    
    # Sampling
    sample_rate: float = 1.0
    
    # Batching and buffering
    batch_size: int = 512
    buffer_capacity: int = 4096
    export_interval_ms: int = 5000
    export_timeout_ms: int = 30000
    
    # Compression (gzip)
    compression: bool = True
    
    # Logging
    log_level: str = "WARNING"
    
    # Resource attributes
    resource_attributes: dict[str, Any] = field(default_factory=dict)
    
    # HTTP headers
    headers: dict[str, str] = field(default_factory=dict)
    
    @classmethod
    def from_env(cls) -> 'ProbeConfig':
        """Create configuration from environment variables."""
        # Parse headers from GATE_HEADERS (format: "key1=value1,key2=value2")
        headers = {}
        headers_str = os.getenv("GATE_HEADERS", "")
        if headers_str:
            for pair in headers_str.split(","):
                if "=" in pair:
                    key, value = pair.split("=", 1)
                    headers[key.strip()] = value.strip()
        
        # Check for API key
        api_key = os.getenv("GATE_API_KEY")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        
        return cls(
            enabled=_get_bool_env("GATE_ENABLED", True),
            endpoint=os.getenv("GATE_ENDPOINT", "http://localhost:4318/v1/traces"),
            service_name=os.getenv("GATE_SERVICE_NAME", 
                                   os.getenv("OTEL_SERVICE_NAME", "unknown-service")),
            service_version=os.getenv("GATE_SERVICE_VERSION", "0.0.0"),
            environment=os.getenv("GATE_ENVIRONMENT", "development"),
            sample_rate=_get_float_env("GATE_SAMPLE_RATE", 1.0),
            batch_size=_get_int_env("GATE_BATCH_SIZE", 512),
            buffer_capacity=_get_int_env("GATE_BUFFER_CAPACITY", 4096),
            export_interval_ms=_get_int_env("GATE_EXPORT_INTERVAL_MS", 5000),
            export_timeout_ms=_get_int_env("GATE_EXPORT_TIMEOUT_MS", 30000),
            compression=_get_bool_env("GATE_COMPRESSION", True),
            log_level=os.getenv("GATE_LOG_LEVEL", "WARNING"),
            headers=headers,
        )
    
    def validate(self) -> None:
        """
        Validate configuration values.
        
        Raises:
            ValueError: If any configuration value is invalid
        """
        if not 0.0 <= self.sample_rate <= 1.0:
            raise ValueError(f"sample_rate must be between 0.0 and 1.0, got {self.sample_rate}")
        
        if self.batch_size < 1:
            raise ValueError(f"batch_size must be at least 1, got {self.batch_size}")
        
        if self.buffer_capacity < 2:
            raise ValueError(f"buffer_capacity must be at least 2, got {self.buffer_capacity}")
        
        if self.export_interval_ms < 100:
            raise ValueError(f"export_interval_ms must be at least 100, got {self.export_interval_ms}")
        
        if self.export_timeout_ms < 1000:
            raise ValueError(f"export_timeout_ms must be at least 1000, got {self.export_timeout_ms}")
    
    def to_dict(self) -> Dict[str, Any]:
        """Convert to dictionary for logging/debugging."""
        return {
            'enabled': self.enabled,
            'endpoint': self.endpoint,
            'service_name': self.service_name,
            'service_version': self.service_version,
            'environment': self.environment,
            'sample_rate': self.sample_rate,
            'batch_size': self.batch_size,
            'buffer_capacity': self.buffer_capacity,
            'export_interval_ms': self.export_interval_ms,
            'export_timeout_ms': self.export_timeout_ms,
            'log_level': self.log_level,
        }


# Global configuration instance
_config: Optional[ProbeConfig] = None
_config_lock = False  # Simple flag, not thread lock (config happens at init)


def get_config() -> ProbeConfig:
    """
    Get the current configuration.
    
    If not explicitly configured, loads from environment variables.
    
    Returns:
        Current ProbeConfig instance
    """
    global _config
    if _config is None:
        _config = ProbeConfig.from_env()
    return _config


def configure(
    *,
    enabled: Optional[bool] = None,
    endpoint: Optional[str] = None,
    service_name: Optional[str] = None,
    service_version: Optional[str] = None,
    environment: Optional[str] = None,
    sample_rate: Optional[float] = None,
    batch_size: Optional[int] = None,
    buffer_capacity: Optional[int] = None,
    export_interval_ms: Optional[int] = None,
    export_timeout_ms: Optional[int] = None,
    compression: Optional[bool] = None,
    log_level: Optional[str] = None,
    resource_attributes: Optional[Dict[str, Any]] = None,
    headers: Optional[Dict[str, str]] = None,
) -> ProbeConfig:
    """
    Configure gate programmatically.
    
    This function should be called BEFORE any instrumented code runs,
    typically at application startup.
    
    Args:
        enabled: Enable/disable instrumentation
        endpoint: OTLP collector endpoint
        service_name: Service name for traces
        service_version: Service version
        environment: Environment tag
        sample_rate: Sampling rate (0.0-1.0)
        batch_size: Spans per export batch
        buffer_capacity: Ring buffer capacity
        export_interval_ms: Export interval
        export_timeout_ms: Export timeout
        compression: Enable gzip compression (default: True)
        log_level: Logging level
        resource_attributes: Additional resource attributes
        headers: HTTP headers for auth
        
    Returns:
        The updated ProbeConfig instance
        
    Example:
        >>> from gate import configure
        >>> configure(
        ...     endpoint="http://reflect-collector:4318/v1/traces",
        ...     service_name="my-agent",
        ...     environment="production",
        ...     sample_rate=0.1,  # Sample 10% in production
        ... )
    """
    global _config, _config_lock
    
    if _config_lock:
        logging.getLogger("gate").warning(
            "Configuration modified after instrumentation started. "
            "Some settings may not take effect."
        )
    
    # Start with current config (or env defaults)
    config = get_config()
    
    # Override with provided values
    if enabled is not None:
        config.enabled = enabled
    if endpoint is not None:
        config.endpoint = endpoint
    if service_name is not None:
        config.service_name = service_name
    if service_version is not None:
        config.service_version = service_version
    if environment is not None:
        config.environment = environment
    if sample_rate is not None:
        config.sample_rate = sample_rate
    if batch_size is not None:
        config.batch_size = batch_size
    if buffer_capacity is not None:
        config.buffer_capacity = buffer_capacity
    if export_interval_ms is not None:
        config.export_interval_ms = export_interval_ms
    if export_timeout_ms is not None:
        config.export_timeout_ms = export_timeout_ms
    if compression is not None:
        config.compression = compression
    if log_level is not None:
        config.log_level = log_level
    if resource_attributes is not None:
        config.resource_attributes.update(resource_attributes)
    if headers is not None:
        config.headers.update(headers)
    
    # Validate
    config.validate()
    
    # Update logging level
    logging.getLogger("gate").setLevel(getattr(logging, config.log_level))
    
    _config = config
    return config


def lock_config() -> None:
    """
    Lock configuration to prevent further changes.
    
    Called internally when instrumentation starts.
    """
    global _config_lock
    _config_lock = True


def reset_config() -> None:
    """
    Reset configuration to defaults.
    
    Primarily for testing purposes.
    """
    global _config, _config_lock
    _config = None
    _config_lock = False
