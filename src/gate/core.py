"""
Tracer and Context Management for gate.

This module provides the core tracing API:
- Tracer: Creates and manages spans
- Context: Thread-local storage for span parent-child relationships
- SpanContext propagation (W3C Trace Context)

Usage:
    from gate import get_tracer
    
    tracer = get_tracer()
    
    with tracer.start_span("my.operation") as span:
        span.set_attribute("key", "value")
        # ... do work ...
    
    # Or for async code:
    async with tracer.start_span("async.operation") as span:
        await do_async_work()

Architecture:
    - Thread-local context stack for automatic parent-child linking
    - Span creation is O(1) with minimal allocations
    - Spans are pushed to ring buffer immediately on end()
    - Sampling decision made at trace creation time
"""

from __future__ import annotations

import atexit
import logging
import random
import signal
import threading
import contextvars
from contextlib import contextmanager
from collections.abc import Callable, Iterator
from typing import Any, TypeVar


from .models import Span, SpanKind, StatusCode, SpanContext
from .structures import BatchingBuffer
from .transport import BatchExporter
from .config import get_config, lock_config, ProbeConfig

logger = logging.getLogger("gate.core")

# Type variable for decorator
F = TypeVar('F', bound=Callable[..., Any])

__all__ = [
    # Core
    "get_tracer",
    "shutdown",
    "force_flush",
    "get_current_span_context",
    # Decorators
    "observe",
    "trace_span",
    # Micro-instrumentation
    "step",
    "StepContext",
    "track",
    "atrack",
    "event",
    "current_span",
    # Agent-native
    "thought",
    "ThoughtContext",
    "guard",
    "Guard",
    "CostBudgetExceeded",
    "LoopBudgetExceeded",
    "snapshot",
    "branch",
    "BranchContext",
]


# Context variable for span context (works across async boundaries)
_current_span_context: contextvars.ContextVar[SpanContext | None] = contextvars.ContextVar(
    'current_span_context', default=None
)

# Thread-local stack for nested spans (for sync code)
_span_stack = threading.local()


def _get_span_stack() -> list[Span]:
    """Get the thread-local span stack."""
    if not hasattr(_span_stack, 'stack'):
        _span_stack.stack = []
    return _span_stack.stack


class Tracer:
    """
    Creates and manages spans for a single instrumentation scope.
    
    The Tracer is the primary interface for creating spans. It handles:
    - Span creation with automatic parent-child linking
    - Sampling decisions
    - Context propagation
    - Span lifecycle management
    
    Thread Safety:
        Tracer instances are thread-safe. Each thread maintains its own
        span stack via thread-local storage.
    
    Attributes:
        name: Instrumentation scope name (e.g., "gate.agent")
        version: Instrumentation version
    """
    
    __slots__ = ('name', 'version', '_buffer', '_config', '_exporter')
    
    def __init__(
        self,
        name: str,
        version: str = "0.1.0",
        buffer: BatchingBuffer[Span] | None = None,
        config: ProbeConfig | None = None,
        exporter: BatchExporter | None = None,
    ) -> None:
        """
        Initialize the tracer.
        
        Args:
            name: Instrumentation scope name
            version: Instrumentation version
            buffer: Span buffer (shared with exporter)
            config: Configuration
            exporter: Background exporter
        """
        self.name = name
        self.version = version
        self._config = config or get_config()
        self._buffer = buffer
        self._exporter = exporter

    def __repr__(self) -> str:
        return f"<Tracer {self.name!r} version={self.version!r}>"
    
    @contextmanager
    def start_span(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: dict[str, Any] | None = None,
        parent: SpanContext | None = None,
    ) -> Iterator[Span]:
        """
        Start a new span as a context manager.
        
        The span is automatically ended when the context exits.
        Parent-child relationships are handled automatically via
        thread-local context.
        
        Args:
            name: Span name (e.g., "agent.execute", "llm.call")
            kind: Span kind (INTERNAL, CLIENT, SERVER, etc.)
            attributes: Initial attributes
            parent: Explicit parent context (auto-detected if None)
            
        Yields:
            The created Span
            
        !!! example "Tracing a Code Block"
            ```python
            with tracer.start_span("agent.think") as span:
                span.set_attribute("thought.iteration", 1)
                result = think()
                span.set_attribute("thought.decision", result)
            ```

        !!! example "Async Tracing"
            ```python
            async with tracer.start_span("llm.call") as span:
                response = await model.generate()
                span.set_attribute("tokens", response.usage)
            ```
        """
        # Check if sampling allows this trace
        if not self._should_sample():
            # Return a no-op span that doesn't record
            yield _NoOpSpan(name)
            return
        
        # Determine parent context
        parent_ctx = parent or _current_span_context.get()
        
        # Also check thread-local stack for sync code
        stack = _get_span_stack()
        if parent_ctx is None and stack:
            parent_span = stack[-1]
            parent_ctx = SpanContext(
                trace_id=parent_span.trace_id,
                span_id=parent_span.span_id,
            )
        
        # Create span
        span = Span(
            name=name,
            trace_id=parent_ctx.trace_id if parent_ctx else None,
            parent_span_id=parent_ctx.span_id if parent_ctx else None,
            kind=kind,
            attributes=attributes,
        )
        
        # Push to stack
        stack.append(span)
        
        # Set context for nested spans
        token = _current_span_context.set(SpanContext(
            trace_id=span.trace_id,
            span_id=span.span_id,
        ))
        
        try:
            yield span
            
            # Mark success if no status set
            if span.status_code == StatusCode.UNSET:
                span.set_status(StatusCode.OK)
                
        except Exception as e:
            # Record exception and mark error
            span.record_exception(e)
            span.set_status(StatusCode.ERROR, str(e))
            raise
            
        finally:
            # End span
            span.end()
            
            # Pop from stack
            if stack and stack[-1] is span:
                stack.pop()
            
            # Reset context
            _current_span_context.reset(token)
            
            # Push to buffer for export
            if self._buffer is not None:
                self._buffer.push(span)
    
    def start_span_no_context(
        self,
        name: str,
        kind: SpanKind = SpanKind.INTERNAL,
        attributes: dict[str, Any] | None = None,
        trace_id: str | None = None,
        parent_span_id: str | None = None,
    ) -> Span:
        """
        Create a span without using context manager.
        
        The caller is responsible for calling span.end() and pushing
        to the buffer. Use this for manual span management.
        
        Args:
            name: Span name
            kind: Span kind
            attributes: Initial attributes
            trace_id: Explicit trace ID
            parent_span_id: Explicit parent span ID
            
        Returns:
            The created Span (must call end() manually)
        """
        return Span(
            name=name,
            trace_id=trace_id,
            parent_span_id=parent_span_id,
            kind=kind,
            attributes=attributes,
        )
    
    def _should_sample(self) -> bool:
        """
        Determine if this trace should be sampled.
        
        Uses head-based sampling: decision made at trace start.
        """
        if self._config.sample_rate >= 1.0:
            return True
        if self._config.sample_rate <= 0.0:
            return False
        return random.random() < self._config.sample_rate


class _NoOpSpan:
    """
    A no-op span for when sampling rejects the trace.
    
    All operations are no-ops. Used to avoid None checks in
    instrumented code.
    """
    
    __slots__ = ('name', 'trace_id', 'span_id', 'attributes')
    
    def __init__(self, name: str) -> None:
        self.name = name
        self.trace_id = ""
        self.span_id = ""
        self.attributes: dict[str, Any] = {}
    
    def set_attribute(self, key: str, value: Any) -> _NoOpSpan:
        return self
    
    def set_attributes(self, attributes: dict[str, Any]) -> _NoOpSpan:
        return self
    
    def add_event(self, name: str, attributes: dict[str, Any] | None = None, 
                  timestamp_ns: int | None = None) -> _NoOpSpan:
        return self
    
    def record_exception(self, exception: BaseException, 
                         escaped: bool = True,
                         attributes: dict[str, Any] | None = None) -> _NoOpSpan:
        return self
    
    def set_status(self, code: StatusCode, message: str | None = None) -> _NoOpSpan:
        return self
    
    def end(self, end_time_ns: int | None = None) -> None:
        pass


# Global tracer provider
class TracerProvider:
    """
    Singleton provider for tracers.
    
    Manages the shared buffer, exporter, and configuration.
    Use get_tracer() instead of instantiating directly.
    """
    
    _instance: TracerProvider | None = None
    _lock = threading.Lock()
    _atexit_registered = False
    
    def __init__(self) -> None:
        self._config = get_config()
        self._buffer: BatchingBuffer[Span] | None = None
        self._exporter: BatchExporter | None = None
        self._tracers: dict[str, Tracer] = {}
        self._initialized = False
        self._shutting_down = False
    
    @classmethod
    def get_instance(cls) -> TracerProvider:
        """Get or create the singleton instance."""
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = cls()
        return cls._instance
    
    @classmethod
    def reset(cls) -> None:
        """Reset the provider (for testing)."""
        with cls._lock:
            if cls._instance is not None:
                cls._instance.shutdown()
            cls._instance = None
    
    def _register_shutdown_handlers(self) -> None:
        """
        Register atexit and signal handlers for graceful shutdown.
        
        This ensures spans are flushed even if the user doesn't call shutdown().
        Critical for data integrity - we don't want to lose the last batch.
        """
        if TracerProvider._atexit_registered:
            return
        
        def _atexit_handler() -> None:
            """Atexit handler for graceful shutdown."""
            if self._initialized and not self._shutting_down:
                logger.debug("Performing graceful shutdown via atexit...")
                self.shutdown(timeout_ms=3000)  # Shorter timeout for atexit
        
        def _signal_handler(signum: int, frame: Any) -> None:
            """SIGTERM handler for graceful shutdown."""
            if self._initialized and not self._shutting_down:
                logger.debug(f"Received signal {signum}, performing graceful shutdown...")
                self.shutdown(timeout_ms=3000)
                # Re-raise the signal after cleanup (for container orchestrators)
                signal.signal(signum, signal.SIG_DFL)
                signal.raise_signal(signum)
        
        # Register atexit handler
        atexit.register(_atexit_handler)
        
        # Register SIGTERM handler (for container shutdown)
        # Don't override SIGINT - let KeyboardInterrupt work normally
        try:
            signal.signal(signal.SIGTERM, _signal_handler)
        except (ValueError, OSError):
            # Can't set signal handler in non-main thread
            pass
        
        TracerProvider._atexit_registered = True
        logger.debug("Registered graceful shutdown handlers (atexit + SIGTERM)")
    
    def initialize(self) -> None:
        """
        Initialize the provider.
        
        Creates buffer and starts exporter thread.
        Should be called once at application startup.
        
        Automatically registers atexit and SIGTERM handlers for graceful shutdown.
        """
        if self._initialized:
            return
        
        if not self._config.enabled:
            logger.info("gate disabled via configuration")
            self._initialized = True
            return
        
        # Lock config to prevent changes after init
        lock_config()
        
        # Create buffer
        self._buffer = BatchingBuffer[Span](
            capacity=self._config.buffer_capacity,
            batch_size=self._config.batch_size,
        )
        
        # Create and start exporter
        self._exporter = BatchExporter(
            buffer=self._buffer,
            config=self._config,
        )
        self._exporter.start()
        
        # Register graceful shutdown handlers
        self._register_shutdown_handlers()
        
        self._initialized = True
        logger.info(f"gate initialized. Endpoint: {self._config.endpoint}")
    
    def shutdown(self, timeout_ms: int = 5000) -> None:
        """
        Shutdown the provider.
        
        Flushes pending spans and stops exporter.
        Thread-safe and idempotent.
        """
        if self._shutting_down:
            return  # Already shutting down
        
        self._shutting_down = True
        
        if self._exporter is not None:
            self._exporter.shutdown(timeout_ms)
        
        self._initialized = False
        logger.info("gate shutdown complete")
    
    def get_tracer(self, name: str, version: str = "0.1.0") -> Tracer:
        """
        Get or create a tracer.
        
        Args:
            name: Instrumentation scope name
            version: Instrumentation version
            
        Returns:
            Tracer instance
        """
        # Initialize on first tracer request
        if not self._initialized:
            self.initialize()
        
        key = f"{name}:{version}"
        if key not in self._tracers:
            self._tracers[key] = Tracer(
                name=name,
                version=version,
                buffer=self._buffer,
                config=self._config,
                exporter=self._exporter,
            )
        
        return self._tracers[key]
    
    def force_flush(self) -> int:
        """Force flush all pending spans."""
        if self._exporter is not None:
            return self._exporter.force_flush()
        return 0
    
    @property
    def stats(self) -> dict[str, Any]:
        """Get export statistics."""
        if self._exporter is not None:
            return self._exporter.stats
        return {}


def get_tracer(name: str = "gate", version: str = "0.1.0") -> Tracer:
    """
    Get a tracer from the global provider.
    
    This is the primary entry point for creating spans.
    
    Args:
        name: Instrumentation scope name
        version: Instrumentation version
        
    Returns:
        Tracer instance
        
    Example:
        tracer = get_tracer("my_module")
        with tracer.start_span("operation") as span:
            # do work
            pass
    """
    return TracerProvider.get_instance().get_tracer(name, version)


def get_current_span_context() -> SpanContext | None:
    """
    Get the current span context.
    
    Useful for context propagation across service boundaries.
    
    Returns:
        Current SpanContext or None if no active span
    """
    return _current_span_context.get()


def shutdown(timeout_ms: int = 5000) -> None:
    """
    Shutdown the tracer provider.
    
    Should be called before program exit to flush pending spans.
    
    Args:
        timeout_ms: Maximum time to wait for flush
    """
    TracerProvider.get_instance().shutdown(timeout_ms)


def force_flush() -> int:
    """
    Force flush all pending spans.
    
    Returns:
        Number of spans flushed
    """
    return TracerProvider.get_instance().force_flush()


# =============================================================================
# @observe DECORATOR
# =============================================================================
# Optimized for minimal overhead:
# 1. Pre-computes function metadata at decoration time.
# 2. Uses Ring Buffer for zero-allocation logging.
# 3. Defers serialization to background thread.
# =============================================================================

class _ObserveDecorator:
    """
    Implementation for high-performance span instrumentation.
    
    See `observe` function for usage.
    """
    
    __slots__ = (
        '_name', '_kind', '_capture_args', '_capture_result', 
        '_static_attrs', '_func', '_is_async', '_func_name',
        '_arg_names', '_tracer', '_code_attrs'
    )
    
    def __init__(
        self,
        name: str | None = None,
        kind: SpanKind = SpanKind.INTERNAL,
        capture_args: bool = False,
        capture_result: bool = False,
        attributes: dict[str, Any] | None = None,
    ) -> None:
        """Initialize the decorator with pre-computed settings."""
        self._name = name
        self._kind = kind
        self._capture_args = capture_args
        self._capture_result = capture_result
        # Freeze static attributes at decoration time
        self._static_attrs = dict(attributes) if attributes else None
        self._func: Callable[..., Any] | None = None
        self._is_async = False
        self._func_name = ""
        self._arg_names: tuple = ()
        self._tracer: Tracer | None = None
        self._code_attrs: dict[str, Any] | None = None

    def _prepare_wrapper(self, func: Callable):
        """Pre-compute all introspection data."""
        import inspect
        
        self._func_name = func.__name__
        self._is_async = inspect.iscoroutinefunction(func)
        
        # Pre-compute source code location for grouping
        try:
            source_file = inspect.getfile(func)
            source_lines = inspect.getsourcelines(func)
            self._code_attrs = {
                'code.function': func.__name__,
                'code.filepath': source_file,
                'code.lineno': source_lines[1] if source_lines else 0,
                'code.namespace': func.__module__ if hasattr(func, '__module__') else '',
            }
        except (TypeError, OSError):
            self._code_attrs = {
                'code.function': func.__name__,
            }
        
        # Pre-compute argument names for capture_args
        if self._capture_args:
            sig = inspect.signature(func)
            self._arg_names = tuple(sig.parameters.keys())
        
        # Resolve span name once
        if not self._name:
            self._name = self._func_name
        
        # Get tracer once (singleton, cached)
        self._tracer = get_tracer("gate.observe")
    
    def __call__(self, func: F) -> F:
        """
        Called when decorating a function.
        
        Pre-computes all introspection at decoration time, NOT call time.
        """
        import functools
        import inspect
        
        self._func = func
        self._func_name = func.__name__
        self._is_async = inspect.iscoroutinefunction(func)
        
        # Pre-compute source code location for grouping
        try:
            source_file = inspect.getfile(func)
            source_lines = inspect.getsourcelines(func)
            self._code_attrs = {
                'code.function': func.__name__,
                'code.filepath': source_file,
                'code.lineno': source_lines[1] if source_lines else 0,
                'code.namespace': func.__module__ if hasattr(func, '__module__') else '',
            }
        except (TypeError, OSError):
            self._code_attrs = {
                'code.function': func.__name__,
            }
        
        # Pre-compute argument names for capture_args
        if self._capture_args:
            sig = inspect.signature(func)
            self._arg_names = tuple(sig.parameters.keys())
        
        # Resolve span name once
        span_name = self._name or self._func_name
        
        # Get tracer once (singleton, cached)
        self._tracer = get_tracer("gate.observe")
        
        if self._is_async:
            @functools.wraps(func)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Start span - O(1) operation
                with self._tracer.start_span(span_name, kind=self._kind) as span:
                    # Add code location attributes (for file-based grouping)
                    if self._code_attrs:
                        span.set_attributes(self._code_attrs)
                    
                    # Add static attributes (pre-frozen dict, no allocation)
                    if self._static_attrs:
                        span.set_attributes(self._static_attrs)
                    
                    # Capture args if requested (uses pre-computed arg names)
                    if self._capture_args:
                        self._set_arg_attributes(span, args, kwargs)
                    
                    # Execute original function
                    result = await func(*args, **kwargs)
                    
                    # Capture result if requested
                    if self._capture_result:
                        span.set_attribute(
                            'function.result', 
                            _truncate_observe(str(result), 1000)
                        )
                    
                    return result
            
            return async_wrapper  # type: ignore
        else:
            @functools.wraps(func)
            def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
                # Start span - O(1) operation
                with self._tracer.start_span(span_name, kind=self._kind) as span:
                    # Add code location attributes (for file-based grouping)
                    if self._code_attrs:
                        span.set_attributes(self._code_attrs)
                    
                    # Add static attributes (pre-frozen dict, no allocation)
                    if self._static_attrs:
                        span.set_attributes(self._static_attrs)
                    
                    # Capture args if requested
                    if self._capture_args:
                        self._set_arg_attributes(span, args, kwargs)
                    
                    # Execute original function
                    result = func(*args, **kwargs)
                    
                    # Capture result if requested
                    if self._capture_result:
                        span.set_attribute(
                            'function.result',
                            _truncate_observe(str(result), 1000)
                        )
                    
                    return result
            
            return sync_wrapper  # type: ignore
    
    def _set_arg_attributes(
        self, 
        span: Span, 
        args: tuple, 
        kwargs: dict[str, Any]
    ) -> None:
        """
        Set function arguments as span attributes.
        
        Uses pre-computed arg_names to avoid introspection at call time.
        Truncates values to prevent bloat.
        """
        # Positional args
        for i, (name, value) in enumerate(zip(self._arg_names, args)):
            if name != 'self':  # Skip self parameter
                span.set_attribute(
                    f'function.arg.{name}',
                    _truncate_observe(str(value), 1000)
                )
        
        # Keyword args
        for key, value in kwargs.items():
            span.set_attribute(
                f'function.arg.{key}',
                _truncate_observe(str(value), 1000)
            )


def _truncate_observe(s: str, max_len: int) -> str:
    """Truncate string for observe decorator. Inline for performance."""
    if len(s) <= max_len:
        return s
    return s[:max_len - 3] + "..."


def _get_caller_info(stack_level: int = 2) -> dict[str, Any]:
    """
    Get code location info from the call stack.
    
    Used by all decorators/context managers to capture where they were called from.
    This enables file-based grouping in dashboards.
    
    Args:
        stack_level: How far up the stack to look (2 = immediate caller)
    
    Returns:
        Dict with code.* attributes for the span
    """
    import inspect
    try:
        frame = inspect.currentframe()
        for _ in range(stack_level):
            if frame is not None:
                frame = frame.f_back
        
        if frame is not None:
            return {
                'code.filepath': frame.f_code.co_filename,
                'code.function': frame.f_code.co_name,
                'code.lineno': frame.f_lineno,
                'code.namespace': frame.f_globals.get('__name__', ''),
            }
    except Exception:
        pass
    finally:
        del frame  # Avoid reference cycles
    
    return {}


# Convenience alias for cleaner imports



def observe(
    name: str | Callable | None = None,
    kind: SpanKind = SpanKind.INTERNAL,
    capture_args: bool = False,
    capture_result: bool = False,
    attributes: dict[str, Any] | None = None,
) -> Callable | _ObserveDecorator:
    """
    High-performance decorator for manual span instrumentation.
    
    Supports both:
        @observe
        def func(): ...
        
        @observe("name", capture_args=True)
        def func(): ...

    See `_ObserveDecorator` for implementation details.
    """
    if callable(name):
        # Case: @observe (bare)
        # Initialize decorator with defaults and immediately wrap function
        decorator = _ObserveDecorator(
            name=None, 
            kind=kind, 
            capture_args=capture_args, 
            capture_result=capture_result, 
            attributes=attributes
        )
        return decorator(name)
    else:
        # Case: @observe("name")
        return _ObserveDecorator(
            name=name,
            kind=kind,
            capture_args=capture_args,
            capture_result=capture_result,
            attributes=attributes
        )


# =============================================================================
# MICRO-INSTRUMENTATION: step(), track(), event()
# =============================================================================
# These complement @observe for tracking granular actions INSIDE functions.
# All use the same Ring Buffer path — zero allocations, <0.1ms overhead.
# =============================================================================

@contextmanager
def step(
    name: str,
    kind: SpanKind = SpanKind.INTERNAL,
    attributes: dict[str, Any] | None = None,
) -> Iterator[StepContext]:
    """
    Context manager for tracking a block of code as a child span.
    
    This is the primary way to track granular actions inside a function.
    Creates a child span under the current span context.
    
    Performance: <0.1ms overhead (same Ring Buffer path as @observe)
    
    Args:
        name: Span name for this step
        kind: SpanKind (default: INTERNAL)
        attributes: Initial attributes (optional)
    
    Yields:
        StepContext with helper methods for recording data

    !!! example
        ```python
        @observe("agent_run")
        async def my_agent(query: str):
            with step("fetch_data") as s:
                data = await fetch(query)
                s.set("rows_fetched", len(data))
        ```
    """
    tracer = get_tracer("gate.step")
    
    # Get caller info for file-based grouping
    caller_info = _get_caller_info(stack_level=2)
    
    # Merge caller info with provided attributes
    merged_attrs = {**caller_info}
    if attributes:
        merged_attrs.update(attributes)
    
    with tracer.start_span(name, kind=kind, attributes=merged_attrs) as span:
        context = StepContext(span)
        try:
            yield context
        except Exception as e:
            context.error(e)
            raise


class StepContext:
    """
    Helper class for the step() context manager.
    
    Provides convenient methods for recording data during a step.
    Uses __slots__ to minimize memory footprint.
    """
    
    __slots__ = ('_span',)
    
    def __init__(self, span: Span) -> None:
        self._span = span
    
    def set(self, key: str, value: Any) -> StepContext:
        """
        Set an attribute on this step's span.
        
        Fluent API - returns self for chaining.
        
        Usage:
            with step("process") as s:
                s.set("input_size", len(data)).set("mode", "fast")
        """
        self._span.set_attribute(key, value)
        return self
    
    def set_many(self, attributes: dict[str, Any]) -> StepContext:
        """Set multiple attributes at once."""
        self._span.set_attributes(attributes)
        return self
    
    def event(self, name: str, attributes: dict[str, Any] | None = None) -> StepContext:
        """
        Record a point-in-time event within this step.
        
        Usage:
            with step("retry_loop") as s:
                for attempt in range(3):
                    s.event("attempt", {"number": attempt})
                    if try_operation():
                        break
        """
        self._span.add_event(name, attributes)
        return self
    
    def error(self, exception: BaseException) -> StepContext:
        """Record an exception on this step."""
        self._span.record_exception(exception)
        self._span.set_status(StatusCode.ERROR, str(exception))
        return self
    
    @property
    def span(self) -> Span:
        """Access the underlying span for advanced operations."""
        return self._span


def track(
    name: str,
    fn: Callable[..., Any],
    *args: Any,
    _kind: SpanKind = SpanKind.INTERNAL,
    _attributes: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """
    One-liner to track a single function call as a span.
    
    Args:
        name: Span name
        fn: Function to call
        *args: Positional arguments to pass to fn
        _kind: SpanKind (use underscore prefix to avoid collision with kwargs)
        _attributes: Initial span attributes
        **kwargs: Keyword arguments to pass to fn
    
    Returns:
        Whatever fn returns

    !!! example
        ```python
        data = track("fetch", fetch_data, query, timeout=30)
        ```
    """
    tracer = get_tracer("gate.track")
    
    # Get caller info for file-based grouping
    caller_info = _get_caller_info(stack_level=2)
    
    # Add the tracked function info
    caller_info['code.tracked_function'] = fn.__name__ if hasattr(fn, '__name__') else str(fn)
    
    # Merge with provided attributes
    merged_attrs = {**caller_info}
    if _attributes:
        merged_attrs.update(_attributes)
    
    with tracer.start_span(name, kind=_kind, attributes=merged_attrs) as span:
        try:
            result = fn(*args, **kwargs)
            return result
        except Exception as e:
            span.record_exception(e)
            span.set_status(StatusCode.ERROR, str(e))
            raise


async def atrack(
    name: str,
    fn: Callable[..., Any],
    *args: Any,
    _kind: SpanKind = SpanKind.INTERNAL,
    _attributes: dict[str, Any] | None = None,
    **kwargs: Any,
) -> Any:
    """
    Async version of track() for coroutines.
    
    !!! example
        ```python
        result = await atrack("fetch", async_fetch, url)
        ```
    """
    tracer = get_tracer("gate.track")
    
    # Get caller info for file-based grouping
    caller_info = _get_caller_info(stack_level=2)
    
    # Add the tracked function info
    caller_info['code.tracked_function'] = fn.__name__ if hasattr(fn, '__name__') else str(fn)
    
    # Merge with provided attributes
    merged_attrs = {**caller_info}
    if _attributes:
        merged_attrs.update(_attributes)
    
    with tracer.start_span(name, kind=_kind, attributes=merged_attrs) as span:
        try:
            result = await fn(*args, **kwargs)
            return result
        except Exception as e:
            span.record_exception(e)
            span.set_status(StatusCode.ERROR, str(e))
            raise


def event(
    name: str,
    attributes: dict[str, Any] | None = None,
) -> None:
    """
    Record a point-in-time event on the current span.
    
    Events are like log entries attached to a span. They have no duration,
    just a timestamp.
    
    Args:
        name: Event name
        attributes: Event attributes (optional)

    !!! example
        ```python
        event("received_query", {"length": len(query)})
        ```
    """
    # Get current span from context
    context = _current_span_context.get()
    if context is None:
        # No active span - silently ignore (fail-open)
        return
    
    # Get the span from the thread-local stack
    stack = _get_span_stack()
    if stack:
        span = stack[-1]
        # Get caller info and merge with provided attributes
        caller_info = _get_caller_info(stack_level=2)
        merged_attrs = {**caller_info}
        if attributes:
            merged_attrs.update(attributes)
        span.add_event(name, merged_attrs)


def current_span() -> Span | None:
    """
    Get the currently active span.
    
    Returns None if no span is active. Use for advanced scenarios
    where you need direct span access.
    
    Usage:
        @observe("my_function")
        def my_function():
            span = current_span()
            if span:
                span.set_attribute("custom.metric", calculate_metric())
    """
    stack = _get_span_stack()
    return stack[-1] if stack else None


# =============================================================================
# UNIQUE DIFFERENTIATORS: Agent-Native Observability
# =============================================================================
# These features do NOT exist in Langfuse, Datadog, New Relic, or OpenLLMetry.
# They leverage gate's position INSIDE the agent runtime to capture
# semantics that HTTP-level instrumentation cannot see.
# =============================================================================

@contextmanager
def thought(
    reasoning: str,
    decision: str | None = None,
    confidence: float | None = None,
    alternatives: list[str] | None = None,
    iteration: int | None = None,
) -> Iterator[ThoughtContext]:
    """
    🧠 UNIQUE: Capture structured Chain-of-Thought reasoning.

    Args:
        reasoning: The agent's internal reasoning (required)
        decision: What action was decided (optional)
        confidence: Self-assessed confidence 0.0-1.0 (optional)
        alternatives: Other options considered (optional)
        iteration: Loop iteration number (optional)
    
    Yields:
        ThoughtContext for recording outcomes

    !!! example
        ```python
        with thought(
            reasoning="User wants stock price.",
            decision="call_tool:get_stock_price",
            confidence=0.92
        ) as t:
            result = await execute(t.decision)
            t.observe_outcome(success=True)
        ```
    """
    tracer = get_tracer("gate.thought")
    
    # Get caller info for file-based grouping
    caller_info = _get_caller_info(stack_level=2)
    
    attrs: Dict[str, Any] = {
        **caller_info,
        'thought.reasoning': _truncate_observe(reasoning, 4000),
        'thought.type': 'chain_of_thought',
    }
    
    if decision is not None:
        attrs['thought.decision'] = decision
    if confidence is not None:
        attrs['thought.confidence'] = max(0.0, min(1.0, confidence))
    if alternatives is not None:
        attrs['thought.alternatives'] = str(alternatives)[:1000]
    if iteration is not None:
        attrs['thought.iteration'] = iteration
    
    with tracer.start_span("agent.thought", kind=SpanKind.INTERNAL, attributes=attrs) as span:
        context = ThoughtContext(span, decision)
        try:
            yield context
        except Exception as e:
            context.observe_outcome(success=False, error=str(e))
            raise


class ThoughtContext:
    """Context for thought() - tracks reasoning outcomes."""
    
    __slots__ = ('_span', 'decision', '_outcome_recorded')
    
    def __init__(self, span: Span, decision: Optional[str]) -> None:
        self._span = span
        self.decision = decision
        self._outcome_recorded = False
    
    def observe_outcome(
        self,
        success: bool,
        result: Optional[Any] = None,
        error: Optional[str] = None,
    ) -> 'ThoughtContext':
        """Record the outcome of this thought's decision."""
        self._outcome_recorded = True
        self._span.set_attribute('thought.outcome.success', success)
        if result is not None:
            self._span.set_attribute('thought.outcome.result', _truncate_observe(str(result), 1000))
        if error is not None:
            self._span.set_attribute('thought.outcome.error', error)
        return self
    
    def refine(self, new_reasoning: str, new_confidence: Optional[float] = None) -> 'ThoughtContext':
        """Refine the thought mid-execution (for iterative reasoning)."""
        self._span.add_event("thought.refined", {
            "new_reasoning": _truncate_observe(new_reasoning, 2000),
            **({"new_confidence": new_confidence} if new_confidence else {}),
        })
        return self


class CostBudgetExceeded(Exception):
    """Raised when agent exceeds cost budget."""
    pass


class LoopBudgetExceeded(Exception):
    """Raised when agent exceeds iteration budget."""
    pass


class Guard:
    """
    💰 UNIQUE: Cost and loop budgets with automatic enforcement.
    
    NO OTHER TOOL HAS THIS. Langfuse tracks cost but can't STOP runaway agents.
    
    This guard can:
    - Track cumulative cost within a scope
    - Enforce maximum iterations (prevent infinite loops)
    - Auto-raise exception if budget exceeded
    - Record budget utilization metrics
    
    Usage:
        @observe("expensive_agent")
        async def my_agent(query: str):
            with guard(max_cost_usd=0.50, max_iterations=10) as g:
                for i in range(100):  # Will stop at 10
                    g.iteration()  # Increments and checks
                    
                    response = await llm.generate(prompt)
                    g.add_cost(response.usage.total_tokens * 0.00001)
                    
                    if done:
                        break
                
                # Check utilization
                print(f"Used ${g.cost_used:.4f} of ${g.max_cost:.2f} budget")
    """
    
    __slots__ = (
        '_span', 'max_cost', 'max_iterations', '_cost_used', 
        '_iterations', '_enforce', '_warned_cost', '_warned_iter'
    )
    
    def __init__(
        self,
        max_cost_usd: Optional[float] = None,
        max_iterations: Optional[int] = None,
        enforce: bool = True,
    ) -> None:
        self._span: Optional[Span] = None
        self.max_cost = max_cost_usd
        self.max_iterations = max_iterations
        self._cost_used = 0.0
        self._iterations = 0
        self._enforce = enforce
        self._warned_cost = False
        self._warned_iter = False
    
    def _check_cost(self) -> None:
        if self.max_cost is None:
            return
        
        utilization = self._cost_used / self.max_cost
        
        # Warn at 80%
        if utilization >= 0.8 and not self._warned_cost:
            self._warned_cost = True
            if self._span:
                self._span.add_event("guard.cost_warning", {
                    "utilization_percent": utilization * 100,
                    "cost_used": self._cost_used,
                    "max_cost": self.max_cost,
                })
        
        # Enforce at 100%
        if self._cost_used > self.max_cost and self._enforce:
            if self._span:
                self._span.set_attribute('guard.budget_exceeded', 'cost')
                self._span.set_status(StatusCode.ERROR, f"Cost budget exceeded: ${self._cost_used:.4f} > ${self.max_cost:.2f}")
            raise CostBudgetExceeded(f"Cost budget exceeded: ${self._cost_used:.4f} > ${self.max_cost:.2f}")
    
    def _check_iterations(self) -> None:
        if self.max_iterations is None:
            return
        
        utilization = self._iterations / self.max_iterations
        
        # Warn at 80%
        if utilization >= 0.8 and not self._warned_iter:
            self._warned_iter = True
            if self._span:
                self._span.add_event("guard.iteration_warning", {
                    "utilization_percent": utilization * 100,
                    "iterations": self._iterations,
                    "max_iterations": self.max_iterations,
                })
        
        # Enforce at 100%
        if self._iterations > self.max_iterations and self._enforce:
            if self._span:
                self._span.set_attribute('guard.budget_exceeded', 'iterations')
                self._span.set_status(StatusCode.ERROR, f"Loop budget exceeded: {self._iterations} > {self.max_iterations}")
            raise LoopBudgetExceeded(f"Loop budget exceeded: {self._iterations} > {self.max_iterations}")
    
    def add_cost(self, cost_usd: float) -> 'Guard':
        """Add cost and check budget."""
        self._cost_used += cost_usd
        self._check_cost()
        return self
    
    def iteration(self) -> int:
        """Increment iteration counter and check budget. Returns current count."""
        self._iterations += 1
        self._check_iterations()
        return self._iterations
    
    @property
    def cost_used(self) -> float:
        return self._cost_used
    
    @property
    def iterations_used(self) -> int:
        return self._iterations
    
    @property
    def cost_remaining(self) -> Optional[float]:
        return (self.max_cost - self._cost_used) if self.max_cost else None
    
    @property
    def iterations_remaining(self) -> Optional[int]:
        return (self.max_iterations - self._iterations) if self.max_iterations else None


@contextmanager
def guard(
    max_cost_usd: float | None = None,
    max_iterations: int | None = None,
    enforce: bool = True,
) -> Iterator[Guard]:
    """
    💰 UNIQUE: Create a cost/loop budget guard.
    
    Args:
        max_cost_usd: Maximum allowed cost in USD
        max_iterations: Maximum allowed iterations
        enforce: If True, raises exception on budget exceed. If False, just records.
    
    Yields:
        Guard instance for tracking

    !!! example
        ```python
        with guard(max_cost_usd=1.00, max_iterations=20) as g:
            g.iteration()
            g.add_cost(0.001)
        ```
    """
    tracer = get_tracer("gate.guard")
    
    # Get caller info for file-based grouping
    caller_info = _get_caller_info(stack_level=2)
    
    attrs: Dict[str, Any] = {
        **caller_info,
        'guard.type': 'budget',
    }
    if max_cost_usd is not None:
        attrs['guard.max_cost_usd'] = max_cost_usd
    if max_iterations is not None:
        attrs['guard.max_iterations'] = max_iterations
    attrs['guard.enforce'] = enforce
    
    g = Guard(max_cost_usd, max_iterations, enforce)
    
    with tracer.start_span("agent.guard", kind=SpanKind.INTERNAL, attributes=attrs) as span:
        g._span = span
        try:
            yield g
        finally:
            # Record final utilization
            span.set_attribute('guard.cost_used_usd', g.cost_used)
            span.set_attribute('guard.iterations_used', g.iterations_used)
            if g.max_cost:
                span.set_attribute('guard.cost_utilization_percent', (g.cost_used / g.max_cost) * 100)
            if g.max_iterations:
                span.set_attribute('guard.iteration_utilization_percent', (g.iterations_used / g.max_iterations) * 100)


def snapshot(
    name: str,
    state: dict[str, Any],
    diff_from: dict[str, Any] | None = None,
) -> None:
    """
    📸 UNIQUE: Capture agent state at a point in time.
    
    Args:
        name: Snapshot name (e.g., "after_tool_call", "before_decision")
        state: Dictionary of state to capture
        diff_from: Optional previous state to compute diff
    
    !!! example
        ```python
        snapshot("after_tool_call", memory, diff_from=initial_memory)
        ```
    """
    span = current_span()
    if span is None:
        return
    
    # Get caller info for file-based grouping
    caller_info = _get_caller_info(stack_level=2)
    
    # Serialize state (truncated to prevent bloat)
    state_str = _truncate_observe(str(state), 4000)
    
    event_attrs: Dict[str, Any] = {
        **caller_info,
        'snapshot.name': name,
        'snapshot.state': state_str,
        'snapshot.keys': str(list(state.keys())),
    }
    
    # Compute diff if previous state provided
    if diff_from is not None:
        added = set(state.keys()) - set(diff_from.keys())
        removed = set(diff_from.keys()) - set(state.keys())
        changed = {k for k in state.keys() & diff_from.keys() if state[k] != diff_from[k]}
        
        event_attrs['snapshot.diff.added'] = str(list(added)) if added else "[]"
        event_attrs['snapshot.diff.removed'] = str(list(removed)) if removed else "[]"
        event_attrs['snapshot.diff.changed'] = str(list(changed)) if changed else "[]"
    
    span.add_event(f"snapshot.{name}", event_attrs)


@contextmanager
def branch(
    decision: str,
    alternatives: list[str],
    rationale: str | None = None,
) -> Iterator[BranchContext]:
    """
    🌳 UNIQUE: Track decision branches and alternatives.
    
    Args:
        decision: The chosen action/path
        alternatives: Other options that were considered
        rationale: Why this decision was made
    
    Yields:
        BranchContext for recording outcome

    !!! example
        ```python
        with branch(
            decision="use_calculator",
            alternatives=["use_search", "give_up"]
        ) as b:
            result = await calc()
            b.outcome(success=True)
        ```
    """
    tracer = get_tracer("gate.branch")
    
    # Get caller info for file-based grouping
    caller_info = _get_caller_info(stack_level=2)
    
    attrs: Dict[str, Any] = {
        **caller_info,
        'branch.decision': decision,
        'branch.alternatives': str(alternatives),
        'branch.alternatives_count': len(alternatives),
    }
    if rationale:
        attrs['branch.rationale'] = _truncate_observe(rationale, 1000)
    
    with tracer.start_span("agent.branch", kind=SpanKind.INTERNAL, attributes=attrs) as span:
        context = BranchContext(span, decision)
        try:
            yield context
        except Exception as e:
            context.outcome(success=False, error=str(e))
            raise


class BranchContext:
    """Context for branch() - tracks decision outcomes."""
    
    __slots__ = ('_span', 'decision')
    
    def __init__(self, span: Span, decision: str) -> None:
        self._span = span
        self.decision = decision
    
    def outcome(
        self,
        success: bool,
        result: Optional[Any] = None,
        error: Optional[str] = None,
        would_retry_with: Optional[str] = None,
    ) -> 'BranchContext':
        """
        Record the outcome of this branch.
        
        Args:
            success: Did the decision work?
            result: What was the result?
            error: What went wrong (if failed)?
            would_retry_with: If you could retry, which alternative would you try?
        """
        self._span.set_attribute('branch.outcome.success', success)
        if result is not None:
            self._span.set_attribute('branch.outcome.result', _truncate_observe(str(result), 1000))
        if error is not None:
            self._span.set_attribute('branch.outcome.error', error)
        if would_retry_with is not None:
            self._span.set_attribute('branch.outcome.would_retry_with', would_retry_with)
        return self

trace_span = observe
