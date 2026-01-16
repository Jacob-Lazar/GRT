"""
Sync Daemon for Gate-to-Reflect Span Export.

This module implements a background daemon that periodically exports
telemetry spans from gate's ring buffer to reflect's SQLite storage.

Architecture:
    - Runs on a daemon thread (won't block process exit)
    - Uses circuit breaker for failure isolation
    - Applies gzip compression for bandwidth efficiency
    - Transforms gate Span objects to reflect's schema

Thread Safety:
    Single daemon instance per process. The daemon thread owns all
    export operations. Use start() and shutdown() from any thread.
"""

from __future__ import annotations

import gzip
import json
import logging
import threading
import time
import urllib.request
import urllib.error
from typing import Any, List, Optional, TYPE_CHECKING

from .config import SyncConfig

if TYPE_CHECKING:
    from src.gate.structures import BatchingBuffer
    from src.gate.models import Span

logger = logging.getLogger("sync.daemon")


__all__ = ["SyncDaemon"]


class CircuitState:
    """Circuit breaker states."""
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class _CircuitBreaker:
    """
    Lightweight circuit breaker for sync daemon.
    
    Prevents retry storms when reflect collector is unavailable.
    State machine: CLOSED → OPEN → HALF_OPEN → CLOSED
    """
    
    __slots__ = (
        '_state', '_failure_count', '_success_count', '_last_failure_time',
        '_failure_threshold', '_recovery_timeout_sec', '_success_threshold',
        '_lock'
    )
    
    def __init__(
        self,
        failure_threshold: int = 5,
        recovery_timeout_sec: float = 30.0,
        success_threshold: int = 2,
    ) -> None:
        """
        Initialize circuit breaker.
        
        Args:
            failure_threshold: Consecutive failures before opening circuit
            recovery_timeout_sec: Seconds to wait before testing recovery
            success_threshold: Successes needed in half-open to fully close
        """
        self._state = CircuitState.CLOSED
        self._failure_count = 0
        self._success_count = 0
        self._last_failure_time: float = 0.0
        self._failure_threshold = failure_threshold
        self._recovery_timeout_sec = recovery_timeout_sec
        self._success_threshold = success_threshold
        self._lock = threading.Lock()
    
    def allow_request(self) -> bool:
        """
        Check if a request should be allowed through.
        
        Returns:
            True if request should proceed, False if circuit is open
        """
        with self._lock:
            if self._state == CircuitState.CLOSED:
                return True
            
            if self._state == CircuitState.OPEN:
                elapsed = time.monotonic() - self._last_failure_time
                if elapsed >= self._recovery_timeout_sec:
                    self._state = CircuitState.HALF_OPEN
                    self._success_count = 0
                    logger.info(
                        f"Circuit breaker: OPEN → HALF_OPEN "
                        f"(testing recovery after {elapsed:.1f}s)"
                    )
                    return True
                return False
            
            if self._state == CircuitState.HALF_OPEN:
                return True
            
            return False
    
    def record_success(self) -> None:
        """Record a successful request."""
        with self._lock:
            if self._state == CircuitState.HALF_OPEN:
                self._success_count += 1
                if self._success_count >= self._success_threshold:
                    self._state = CircuitState.CLOSED
                    self._failure_count = 0
                    logger.info(
                        f"Circuit breaker: HALF_OPEN → CLOSED "
                        f"(recovered after {self._success_count} successes)"
                    )
            elif self._state == CircuitState.CLOSED:
                self._failure_count = 0
    
    def record_failure(self) -> None:
        """Record a failed request."""
        with self._lock:
            self._last_failure_time = time.monotonic()
            
            if self._state == CircuitState.HALF_OPEN:
                self._state = CircuitState.OPEN
                logger.warning("Circuit breaker: HALF_OPEN → OPEN (recovery failed)")
            
            elif self._state == CircuitState.CLOSED:
                self._failure_count += 1
                if self._failure_count >= self._failure_threshold:
                    self._state = CircuitState.OPEN
                    logger.warning(
                        f"Circuit breaker: CLOSED → OPEN "
                        f"({self._failure_count} consecutive failures)"
                    )
    
    @property
    def state(self) -> str:
        """Current circuit state."""
        with self._lock:
            return self._state


class SyncDaemon:
    """
    Background daemon that exports spans from gate to reflect.
    
    The daemon runs on a background thread and periodically drains
    spans from gate's BatchingBuffer, transforms them to reflect's
    schema, and sends them via HTTP POST.
    
    Usage:
        ```python
        from src.gate.core import TracerProvider
        from sync import SyncDaemon, SyncConfig
        
        provider = TracerProvider.get_instance()
        daemon = SyncDaemon(
            buffer=provider._buffer,
            config=SyncConfig.from_env(),
        )
        daemon.start()
        
        # ... application runs ...
        
        daemon.shutdown()
        ```
    
    Thread Safety:
        Single instance per process. start() and shutdown() can be
        called from any thread. The export loop runs on a daemon thread.
    """
    
    __slots__ = (
        '_buffer', '_config', '_thread', '_shutdown_event',
        '_circuit_breaker', '_total_synced', '_total_dropped',
        '_sync_errors'
    )
    
    def __init__(
        self,
        buffer: "BatchingBuffer",
        config: Optional[SyncConfig] = None,
    ) -> None:
        """
        Initialize the sync daemon.
        
        Args:
            buffer: Gate's BatchingBuffer to drain spans from
            config: Sync configuration (uses defaults if None)
        """
        self._buffer = buffer
        self._config = config or SyncConfig()
        
        self._thread: Optional[threading.Thread] = None
        self._shutdown_event = threading.Event()
        
        self._circuit_breaker = _CircuitBreaker(
            failure_threshold=5,
            recovery_timeout_sec=30.0,
            success_threshold=2,
        )
        
        # Metrics
        self._total_synced = 0
        self._total_dropped = 0
        self._sync_errors = 0
    
    def __repr__(self) -> str:
        return f"<SyncDaemon endpoint={self._config.reflect_endpoint!r}>"
    
    def start(self) -> None:
        """
        Start the sync daemon thread.
        
        The thread is a daemon, so it won't prevent program exit.
        Call shutdown() to flush pending spans before exit.
        """
        if not self._config.enabled:
            logger.info("SyncDaemon disabled by configuration")
            return
        
        if self._thread is not None and self._thread.is_alive():
            return  # Already running
        
        self._shutdown_event.clear()
        self._thread = threading.Thread(
            target=self._sync_loop,
            name="sync.daemon",
            daemon=True,
        )
        self._thread.start()
        logger.info(
            f"SyncDaemon started: endpoint={self._config.reflect_endpoint}, "
            f"interval={self._config.sync_interval_sec}s"
        )
    
    def shutdown(self, timeout_ms: int = 5000) -> None:
        """
        Shutdown the daemon and flush pending spans.
        
        Args:
            timeout_ms: Maximum time to wait for flush
        """
        if self._thread is None or not self._thread.is_alive():
            return
        
        logger.debug("Shutting down SyncDaemon...")
        self._shutdown_event.set()
        
        self._thread.join(timeout=timeout_ms / 1000)
        
        if self._thread.is_alive():
            logger.warning("SyncDaemon did not shutdown cleanly")
        else:
            logger.info(
                f"SyncDaemon shutdown complete. "
                f"Synced: {self._total_synced}, Dropped: {self._total_dropped}"
            )
    
    def _sync_loop(self) -> None:
        """
        Main sync loop (runs on daemon thread).
        
        Periodically drains spans from buffer and sends to reflect.
        Uses circuit breaker for failure isolation.
        """
        backoff_sec = 1.0
        max_backoff_sec = 60.0
        
        while not self._shutdown_event.is_set():
            try:
                batch = self._buffer.get_batch()
                
                if batch:
                    if not self._circuit_breaker.allow_request():
                        # Circuit open - drop spans
                        self._total_dropped += len(batch)
                        logger.warning(
                            f"Circuit open: dropping {len(batch)} spans"
                        )
                    else:
                        # Attempt sync
                        success = self._send_batch(batch)
                        
                        if success:
                            self._circuit_breaker.record_success()
                            self._total_synced += len(batch)
                            backoff_sec = 1.0
                        else:
                            self._circuit_breaker.record_failure()
                            self._total_dropped += len(batch)
                            self._sync_errors += 1
                            backoff_sec = min(backoff_sec * 2, max_backoff_sec)
                
                # Wait for next interval
                self._shutdown_event.wait(timeout=self._config.sync_interval_sec)
                
            except Exception as e:
                logger.error(f"Sync loop error: {e}", exc_info=True)
                self._circuit_breaker.record_failure()
                self._shutdown_event.wait(timeout=backoff_sec)
        
        # Final flush on shutdown
        self._final_flush()
    
    def _send_batch(self, spans: List["Span"]) -> bool:
        """
        Send a batch of spans to reflect collector.
        
        Args:
            spans: List of gate Span objects
            
        Returns:
            True if successful, False otherwise
        """
        if not spans:
            return True
        
        try:
            # Transform spans to reflect's OTLP format
            payload = self._build_otlp_payload(spans)
            payload_bytes = json.dumps(payload).encode("utf-8")
            
            # Apply compression if enabled
            headers = {"Content-Type": "application/json"}
            if self._config.compression:
                payload_bytes = gzip.compress(payload_bytes, compresslevel=6)
                headers["Content-Encoding"] = "gzip"
            
            # Send HTTP request
            request = urllib.request.Request(
                self._config.reflect_endpoint,
                data=payload_bytes,
                headers=headers,
                method="POST",
            )
            
            timeout_sec = self._config.timeout_ms / 1000
            with urllib.request.urlopen(request, timeout=timeout_sec) as response:
                response.read()
            
            logger.debug(f"Synced {len(spans)} spans to reflect")
            return True
            
        except urllib.error.HTTPError as e:
            logger.warning(f"Sync failed: HTTP {e.code} {e.reason}")
            return False
        except urllib.error.URLError as e:
            logger.warning(f"Sync failed: {e.reason}")
            return False
        except Exception as e:
            logger.error(f"Sync failed: {e}", exc_info=True)
            return False
    
    def _build_otlp_payload(self, spans: List["Span"]) -> dict:
        """
        Build OTLP JSON payload from gate spans.
        
        Args:
            spans: List of gate Span objects
            
        Returns:
            OTLP-formatted payload dict
        """
        otlp_spans = []
        for span in spans:
            otlp_span = self._transform_span(span)
            otlp_spans.append(otlp_span)
        
        return {
            "resourceSpans": [{
                "resource": {
                    "attributes": [
                        {"key": "service.name", "value": {"stringValue": "gate-sync"}},
                    ],
                },
                "scopeSpans": [{
                    "scope": {
                        "name": "sync.daemon",
                        "version": "0.1.0",
                    },
                    "spans": otlp_spans,
                }],
            }],
        }
    
    def _transform_span(self, span: "Span") -> dict:
        """
        Transform a gate Span to OTLP format.
        
        Args:
            span: Gate Span object
            
        Returns:
            OTLP-formatted span dict
        """
        otlp_span: dict[str, Any] = {
            "traceId": span.trace_id,
            "spanId": span.span_id,
            "name": span.name,
            "kind": span.kind + 1,  # OTLP uses 1-indexed SpanKind
            "startTimeUnixNano": str(span.start_time_ns),
            "endTimeUnixNano": str(span.end_time_ns or span.start_time_ns),
            "attributes": [
                self._attr_to_otlp(k, v) for k, v in span.attributes.items()
            ],
            "status": {
                "code": span.status_code,
            },
        }
        
        if span.parent_span_id:
            otlp_span["parentSpanId"] = span.parent_span_id
        
        if span.status_message:
            otlp_span["status"]["message"] = span.status_message
        
        if span.events:
            otlp_span["events"] = [
                {
                    "name": e.name,
                    "timeUnixNano": str(e.timestamp_ns),
                    "attributes": [
                        self._attr_to_otlp(k, v) for k, v in e.attributes.items()
                    ],
                }
                for e in span.events
            ]
        
        if span.links:
            otlp_span["links"] = [
                {
                    "traceId": link.trace_id,
                    "spanId": link.span_id,
                    "attributes": [
                        self._attr_to_otlp(k, v) for k, v in link.attributes.items()
                    ],
                }
                for link in span.links
            ]
        
        return otlp_span
    
    def _attr_to_otlp(self, key: str, value: Any) -> dict:
        """
        Convert attribute to OTLP format.
        
        Args:
            key: Attribute key
            value: Attribute value
            
        Returns:
            OTLP-formatted attribute dict
        """
        attr: dict[str, Any] = {"key": key, "value": {}}
        
        if isinstance(value, bool):
            attr["value"]["boolValue"] = value
        elif isinstance(value, int):
            attr["value"]["intValue"] = str(value)
        elif isinstance(value, float):
            attr["value"]["doubleValue"] = value
        elif isinstance(value, str):
            attr["value"]["stringValue"] = value
        elif isinstance(value, (list, tuple)):
            if value and isinstance(value[0], str):
                attr["value"]["arrayValue"] = {
                    "values": [{"stringValue": v} for v in value]
                }
            elif value and isinstance(value[0], int):
                attr["value"]["arrayValue"] = {
                    "values": [{"intValue": str(v)} for v in value]
                }
            else:
                attr["value"]["stringValue"] = str(value)
        else:
            attr["value"]["stringValue"] = str(value)
        
        return attr
    
    def _final_flush(self) -> None:
        """
        Final flush on shutdown - best-effort export of remaining spans.
        """
        try:
            while True:
                batch = self._buffer.get_batch()
                if not batch:
                    break
                
                if self._send_batch(batch):
                    self._total_synced += len(batch)
                else:
                    self._total_dropped += len(batch)
        except Exception as e:
            logger.error(f"Final flush error: {e}")
    
    @property
    def stats(self) -> dict[str, Any]:
        """
        Sync daemon statistics.
        
        Returns:
            Dict with synced, dropped, errors counts and circuit state
        """
        return {
            "total_synced": self._total_synced,
            "total_dropped": self._total_dropped,
            "sync_errors": self._sync_errors,
            "circuit_state": self._circuit_breaker.state,
            "enabled": self._config.enabled,
            "endpoint": self._config.reflect_endpoint,
        }
