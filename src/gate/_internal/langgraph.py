"""
LangGraph Auto-Instrumentation for gate.

This module automatically instruments LangGraph to:
1. Propagate trace context through StateGraph execution
2. Create spans for each graph node execution
3. Link all node spans as children of a single workflow trace

The Problem:
    LangGraph's internal scheduler doesn't propagate Python's contextvars
    when invoking node functions. This causes each node to create a 
    separate trace instead of being children of a single workflow trace.

The Solution:
    We monkey-patch LangGraph's core execution methods to:
    1. Capture the active span context before graph execution
    2. Inject that context into each node invocation via callbacks
    3. Create proper parent-child span relationships

Supported LangGraph Components:
    - StateGraph.compile() → CompiledGraph
    - CompiledGraph.invoke() / ainvoke() / stream() / astream()
    - Node function execution
    - Conditional edges (routers)

Zero Code Changes Required:
    Simply importing gate and calling configure() is enough.
    This instrumentor is auto-activated by instrument_all().
"""

from __future__ import annotations

import logging
import functools
import time
import contextvars
from typing import Any, Optional, Dict, Callable, TypeVar
from contextlib import contextmanager

from ..core import get_tracer, get_current_span_context, _current_span_context
from ..models import Span, SpanKind, StatusCode, SpanContext

logger = logging.getLogger("gate.langgraph")

F = TypeVar('F', bound=Callable[..., Any])

# Store for propagating context into LangGraph callbacks
_langgraph_context: contextvars.ContextVar[Optional[SpanContext]] = contextvars.ContextVar(
    '_langgraph_context', default=None
)


def _instrument_langgraph() -> bool:
    """
    Instrument LangGraph for automatic context propagation.
    
    This patches:
    1. Pregel.invoke/ainvoke - Creates root workflow span (the real execution class)
    2. Node execution - Creates child spans for each node
    3. Conditional edges - Tracks routing decisions
    
    Note: CompiledStateGraph inherits invoke() from Pregel, so we must
    patch Pregel directly to intercept all graph executions.
    
    Returns:
        True if instrumentation succeeded
    """
    try:
        from langgraph.graph import StateGraph
        from langgraph.graph.state import CompiledStateGraph
        from langgraph.pregel import Pregel  # The actual class with invoke()
        
        # Check if already instrumented (mark on Pregel, not CompiledStateGraph)
        if hasattr(Pregel, '_gate_instrumented'):
            logger.debug("LangGraph already instrumented")
            return True
        
        tracer = get_tracer("gate.langgraph")
        
        # =====================================================================
        # Patch Pregel.invoke() - The real execution class
        # CompiledStateGraph inherits invoke() from Pregel
        # =====================================================================
        original_invoke = Pregel.invoke
        
        @functools.wraps(original_invoke)
        def traced_invoke(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
            # Check if we're already inside a traced workflow (nested invoke)
            existing_ctx = get_current_span_context()
            
            # Create workflow span
            graph_name = getattr(self, 'name', None) or self.__class__.__name__
            span_name = f"langgraph.{graph_name}"
            
            with tracer.start_span(span_name, kind=SpanKind.INTERNAL) as span:
                span.set_attributes({
                    'langgraph.graph_name': graph_name,
                    'langgraph.input_keys': str(list(input.keys()) if isinstance(input, dict) else type(input).__name__),
                })
                
                # Store our span context for node callbacks
                workflow_ctx = SpanContext(
                    trace_id=span.trace_id,
                    span_id=span.span_id,
                )
                token = _langgraph_context.set(workflow_ctx)
                
                try:
                    # Inject our callback handler into config
                    config = _inject_callback_handler(config, workflow_ctx, tracer)
                    
                    start_time = time.time()
                    result = original_invoke(self, input, config, **kwargs)
                    latency_ms = (time.time() - start_time) * 1000
                    
                    span.set_attributes({
                        'langgraph.latency_ms': latency_ms,
                        'langgraph.output_keys': str(list(result.keys()) if isinstance(result, dict) else type(result).__name__),
                    })
                    
                    return result
                    
                except Exception as e:
                    span.set_status(StatusCode.ERROR, str(e))
                    span.record_exception(e)
                    raise
                finally:
                    _langgraph_context.reset(token)
        
        Pregel.invoke = traced_invoke
        
        # =====================================================================
        # Patch Pregel.ainvoke() (async version)
        # =====================================================================
        original_ainvoke = Pregel.ainvoke
        
        @functools.wraps(original_ainvoke)
        async def traced_ainvoke(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
            graph_name = getattr(self, 'name', None) or self.__class__.__name__
            span_name = f"langgraph.{graph_name}"
            
            with tracer.start_span(span_name, kind=SpanKind.INTERNAL) as span:
                span.set_attributes({
                    'langgraph.graph_name': graph_name,
                    'langgraph.input_keys': str(list(input.keys()) if isinstance(input, dict) else type(input).__name__),
                })
                
                workflow_ctx = SpanContext(
                    trace_id=span.trace_id,
                    span_id=span.span_id,
                )
                token = _langgraph_context.set(workflow_ctx)
                
                try:
                    config = _inject_callback_handler(config, workflow_ctx, tracer)
                    
                    start_time = time.time()
                    result = await original_ainvoke(self, input, config, **kwargs)
                    latency_ms = (time.time() - start_time) * 1000
                    
                    span.set_attributes({
                        'langgraph.latency_ms': latency_ms,
                        'langgraph.output_keys': str(list(result.keys()) if isinstance(result, dict) else type(result).__name__),
                    })
                    
                    return result
                    
                except Exception as e:
                    span.set_status(StatusCode.ERROR, str(e))
                    span.record_exception(e)
                    raise
                finally:
                    _langgraph_context.reset(token)
        
        Pregel.ainvoke = traced_ainvoke
        
        # =====================================================================
        # Patch Pregel.stream() 
        # =====================================================================
        original_stream = Pregel.stream
        
        @functools.wraps(original_stream)
        def traced_stream(self: Any, input: Any, config: Any = None, **kwargs: Any):
            """
            Wrapper for Pregel.stream() that creates a workflow span.
            
            Uses start_span_no_context since generators can't use context managers.
            """
            from ..core import TracerProvider
            
            graph_name = getattr(self, 'name', None) or self.__class__.__name__
            span_name = f"langgraph.{graph_name}.stream"
            
            # Get parent context if available
            parent_ctx = get_current_span_context()
            
            # Create span manually (can't use context manager in generator)
            span = tracer.start_span_no_context(
                span_name, 
                kind=SpanKind.INTERNAL,
                trace_id=parent_ctx.trace_id if parent_ctx else None,
                parent_span_id=parent_ctx.span_id if parent_ctx else None,
            )
            span.set_attributes({
                'langgraph.graph_name': graph_name,
                'langgraph.mode': 'stream',
            })
            
            workflow_ctx = SpanContext(
                trace_id=span.trace_id,
                span_id=span.span_id,
            )
            token = _langgraph_context.set(workflow_ctx)
            
            try:
                config = _inject_callback_handler(config, workflow_ctx, tracer)
                
                for chunk in original_stream(self, input, config, **kwargs):
                    yield chunk
                
                # Mark success
                span.set_status(StatusCode.OK)
                    
            except Exception as e:
                span.set_status(StatusCode.ERROR, str(e))
                span.record_exception(e)
                raise
            finally:
                _langgraph_context.reset(token)
                span.end()
                # Push to buffer for export
                provider = TracerProvider.get_instance()
                if provider and provider._buffer is not None:
                    provider._buffer.push(span)
        
        Pregel.stream = traced_stream
        
        # Mark as instrumented on Pregel (the real class)
        Pregel._gate_instrumented = True
        
        # Also try to instrument the base CompiledGraph if different
        try:
            from langgraph.graph.graph import CompiledGraph
            if CompiledGraph is not CompiledStateGraph:
                _instrument_compiled_graph_base(CompiledGraph, tracer)
        except ImportError:
            pass
        
        logger.info("LangGraph instrumented successfully")
        return True
        
    except ImportError:
        logger.debug("LangGraph not installed")
        return False
    except Exception as e:
        logger.warning(f"Failed to instrument LangGraph: {e}")
        import traceback
        traceback.print_exc()
        return False


def _instrument_compiled_graph_base(CompiledGraph: type, tracer: Any) -> None:
    """Instrument base CompiledGraph class if it exists."""
    if hasattr(CompiledGraph, '_gate_instrumented'):
        return
    
    original_invoke = CompiledGraph.invoke
    
    @functools.wraps(original_invoke)
    def traced_invoke(self: Any, input: Any, config: Any = None, **kwargs: Any) -> Any:
        graph_name = getattr(self, 'name', None) or 'CompiledGraph'
        
        with tracer.start_span(f"langgraph.{graph_name}", kind=SpanKind.INTERNAL) as span:
            workflow_ctx = SpanContext(trace_id=span.trace_id, span_id=span.span_id)
            token = _langgraph_context.set(workflow_ctx)
            
            try:
                config = _inject_callback_handler(config, workflow_ctx, tracer)
                return original_invoke(self, input, config, **kwargs)
            finally:
                _langgraph_context.reset(token)
    
    CompiledGraph.invoke = traced_invoke
    CompiledGraph._gate_instrumented = True


def _inject_callback_handler(config: Any, workflow_ctx: SpanContext, tracer: Any) -> Any:
    """
    Inject our callback handler into LangGraph's config.
    
    This handler creates child spans for each node execution.
    """
    try:
        from langchain_core.runnables import RunnableConfig
        from langchain_core.callbacks import BaseCallbackHandler
        
        class GateNodeCallbackHandler(BaseCallbackHandler):
            """
            Callback handler that creates spans for LangGraph node executions.
            
            LangGraph uses LangChain's callback system internally. By injecting
            this handler, we can create properly parented spans for each node.
            """
            
            def __init__(self, parent_ctx: SpanContext, tracer: Any):
                super().__init__()
                self.parent_ctx = parent_ctx
                self.tracer = tracer
                self._spans: Dict[str, Span] = {}
                self._node_start_times: Dict[str, float] = {}
            
            @property
            def raise_error(self) -> bool:
                return False
            
            def on_chain_start(
                self,
                serialized: Dict[str, Any],
                inputs: Dict[str, Any],
                *,
                run_id: Any,
                parent_run_id: Any = None,
                tags: Optional[list] = None,
                metadata: Optional[Dict[str, Any]] = None,
                **kwargs: Any,
            ) -> None:
                """Called when a chain (node) starts."""
                run_id_str = str(run_id)
                
                # serialized can be None for some runnables
                serialized = serialized or {}
                
                # Extract node name from serialized data, tags, or metadata
                name = (
                    serialized.get('name', '') or 
                    serialized.get('id', ['unknown'])[-1] if serialized.get('id') else 'unknown'
                )
                
                # Try tags if name is generic
                if name in ('unknown', 'RunnableLambda') and tags:
                    for tag in tags:
                        if isinstance(tag, str) and not tag.startswith('seq:'):
                            name = tag
                            break
                
                # Skip internal LangGraph chains we don't care about
                if name in ('RunnableSequence', 'RunnableLambda', 'ChannelWrite', 'ChannelRead', 'unknown'):
                    return
                
                # Determine parent span
                parent_span_id = self.parent_ctx.span_id
                if parent_run_id and str(parent_run_id) in self._spans:
                    parent_span_id = self._spans[str(parent_run_id)].span_id
                
                # Create child span for this node
                span = Span(
                    name=f"node.{name}",
                    trace_id=self.parent_ctx.trace_id,
                    parent_span_id=parent_span_id,
                    kind=SpanKind.INTERNAL,
                )
                
                span.set_attributes({
                    'langgraph.node_name': name,
                    'langgraph.run_id': run_id_str,
                })
                
                # Capture input keys (not full values to avoid bloat)
                if isinstance(inputs, dict):
                    span.set_attribute('langgraph.input_keys', str(list(inputs.keys())[:10]))
                
                self._spans[run_id_str] = span
                self._node_start_times[run_id_str] = time.time()
            
            def on_chain_end(
                self,
                outputs: Dict[str, Any],
                *,
                run_id: Any,
                **kwargs: Any,
            ) -> None:
                """Called when a chain (node) ends."""
                run_id_str = str(run_id)
                
                if run_id_str not in self._spans:
                    return
                
                span = self._spans.pop(run_id_str)
                start_time = self._node_start_times.pop(run_id_str, time.time())
                
                latency_ms = (time.time() - start_time) * 1000
                span.set_attribute('langgraph.latency_ms', latency_ms)
                
                # Capture output keys
                if isinstance(outputs, dict):
                    span.set_attribute('langgraph.output_keys', str(list(outputs.keys())[:10]))
                
                span.end()
                
                # Push to buffer for export
                from ..core import TracerProvider
                provider = TracerProvider.get_instance()
                if provider._buffer:
                    provider._buffer.push(span)
            
            def on_chain_error(
                self,
                error: Exception,
                *,
                run_id: Any,
                **kwargs: Any,
            ) -> None:
                """Called when a chain (node) errors."""
                run_id_str = str(run_id)
                
                if run_id_str not in self._spans:
                    return
                
                span = self._spans.pop(run_id_str)
                self._node_start_times.pop(run_id_str, None)
                
                span.set_status(StatusCode.ERROR, str(error))
                span.record_exception(error)
                span.end()
                
                from ..core import TracerProvider
                provider = TracerProvider.get_instance()
                if provider._buffer:
                    provider._buffer.push(span)
        
        # Create handler
        handler = GateNodeCallbackHandler(workflow_ctx, tracer)
        
        # Merge with existing config
        if config is None:
            config = RunnableConfig(callbacks=[handler])
        elif isinstance(config, dict):
            existing_callbacks = config.get('callbacks', []) or []
            config['callbacks'] = existing_callbacks + [handler]
        elif hasattr(config, 'callbacks'):
            existing_callbacks = config.callbacks or []
            config = RunnableConfig(
                **{k: v for k, v in config.__dict__.items() if k != 'callbacks'},
                callbacks=existing_callbacks + [handler]
            )
        else:
            config = RunnableConfig(callbacks=[handler])
        
        return config
        
    except ImportError:
        logger.debug("langchain_core not available for callback injection")
        return config
    except Exception as e:
        logger.debug(f"Failed to inject callback handler: {e}")
        return config


def _instrument_langgraph_pregel() -> bool:
    """
    Instrument LangGraph's Pregel execution engine (lower-level).
    
    This provides tracing even when callbacks aren't propagated.
    """
    try:
        from langgraph.pregel import Pregel
        
        if hasattr(Pregel, '_gate_instrumented'):
            return True
        
        tracer = get_tracer("gate.langgraph.pregel")
        
        # Patch the _execute method which runs nodes
        if hasattr(Pregel, '_execute'):
            original_execute = Pregel._execute
            
            @functools.wraps(original_execute)
            def traced_execute(self: Any, *args: Any, **kwargs: Any) -> Any:
                context = _langgraph_context.get()
                
                if context:
                    # We have workflow context, create child span
                    with tracer.start_span(
                        "pregel.execute",
                        kind=SpanKind.INTERNAL,
                    ) as span:
                        # Manually set parent since we're outside normal context
                        span.trace_id = context.trace_id
                        span.parent_span_id = context.span_id
                        
                        return original_execute(self, *args, **kwargs)
                else:
                    return original_execute(self, *args, **kwargs)
            
            Pregel._execute = traced_execute
        
        Pregel._gate_instrumented = True
        return True
        
    except ImportError:
        return False
    except Exception as e:
        logger.debug(f"Failed to instrument Pregel: {e}")
        return False


def instrument_langgraph() -> bool:
    """
    Public API to instrument LangGraph.
    
    Called automatically by instrument_all(), but can be called
    manually if needed.
    
    Returns:
        True if any LangGraph instrumentation succeeded
    """
    success = False
    
    if _instrument_langgraph():
        success = True
    
    if _instrument_langgraph_pregel():
        success = True
    
    return success
