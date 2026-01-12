"""
Auto-instrumentors for common libraries.

This module provides automatic instrumentation for libraries commonly
used with AI agents:
- OpenAI (chat completions, embeddings)
- Anthropic (messages)
- HTTPX/Requests (HTTP calls)
- LangChain (if installed)
- LlamaIndex (if installed)

The goal is to provide out-of-the-box visibility into LLM calls and
external HTTP requests without requiring code changes.

Instrumentation Strategy:
    We attempt to use OpenLLMetry instrumentors where available,
    falling back to simple monkey-patching for unsupported libraries.
"""

from __future__ import annotations

import logging
import functools
import time
from typing import Any, TypeVar
from collections.abc import Callable

from .core import get_tracer
from .models import SpanKind

logger = logging.getLogger("gate.hooks")

F = TypeVar('F', bound=Callable[..., Any])

__all__ = [
    "instrument_all",
]

def instrument_all() -> None:
    """
    Instrument all supported libraries.
    
    Args:
        None
    
    Examples:
        !!! example "Manual instrumentation setup"
            ```python
            from gate import configure
            from gate.hooks import instrument_all
            
            # Configure first
            configure(service_name="my-agent")
            
            # Then instrument everything
            instrument_all()
            
            # Now OpenAI/Anthropic calls are automatically traced
            ```
    """
    # Track what was instrumented
    instrumented = []
    failed = []
    
    # Try OpenLLMetry instrumentors first (they're well-maintained)
    if _try_openllmetry():
        instrumented.append("OpenLLMetry (OpenAI, Anthropic, LangChain, etc.)")
    else:
        # Fallback to our own instrumentors
        if _instrument_openai():
            instrumented.append("OpenAI")
        else:
            failed.append("OpenAI")
        
        if _instrument_anthropic():
            instrumented.append("Anthropic")
        else:
            failed.append("Anthropic")
    
    # LangGraph instrumentation (context propagation through StateGraph)
    # This is critical - LangGraph breaks contextvars, we fix it
    from .langgraph_instrumentor import instrument_langgraph
    if instrument_langgraph():
        instrumented.append("LangGraph")
    
    # HTTP clients (usually not covered by OpenLLMetry)
    if _instrument_httpx():
        instrumented.append("httpx")
    
    if _instrument_requests():
        instrumented.append("requests")
    
    if instrumented:
        logger.info(f"Instrumented: {', '.join(instrumented)}")
    
    if failed:
        logger.debug(f"Could not instrument (not installed?): {', '.join(failed)}")


def _try_openllmetry() -> bool:
    """
    Try to use OpenLLMetry's instrumentors.
    
    OpenLLMetry provides high-quality instrumentation for many LLM
    providers and frameworks. We use it when available.
    
    Returns:
        True if OpenLLMetry initialized successfully
    """
    try:
        from traceloop.sdk import Traceloop
        from .config import get_config
        
        config = get_config()
        
        # Initialize Traceloop with our endpoint
        Traceloop.init(
            app_name=config.service_name,
            disable_batch=False,  # Use batching
            api_endpoint=config.endpoint.replace('/v1/traces', ''),  # Base URL
            headers=config.headers,
        )
        
        logger.info("OpenLLMetry initialized successfully")
        return True
        
    except ImportError:
        logger.debug("OpenLLMetry not installed, using built-in instrumentors")
        return False
    except Exception as e:
        logger.warning(f"OpenLLMetry init failed: {e}, using built-in instrumentors")
        return False


def _instrument_openai() -> bool:
    """
    Instrument OpenAI library.
    
    Wraps:
        - client.chat.completions.create()
        - client.embeddings.create()
    
    Returns:
        True if instrumentation succeeded
    """
    try:
        import openai
        
        # Check if already instrumented
        if hasattr(openai, '_gate_instrumented'):
            return True
        
        tracer = get_tracer("gate.openai")
        
        # Instrument the completions create method
        original_create = openai.resources.chat.completions.Completions.create
        
        @functools.wraps(original_create)
        def traced_create(self: Any, *args: Any, **kwargs: Any) -> Any:
            model = kwargs.get('model', 'unknown')
            
            with tracer.start_span("llm.call", kind=SpanKind.CLIENT) as span:
                span.set_attributes({
                    'gen_ai.system': 'openai',
                    'gen_ai.request.model': model,
                })
                
                # Capture messages if present (truncated)
                if 'messages' in kwargs:
                    messages = kwargs['messages']
                    span.set_attribute('gen_ai.prompt', _format_messages(messages))
                
                start_time = time.time()
                result = original_create(self, *args, **kwargs)
                latency_ms = (time.time() - start_time) * 1000
                
                span.set_attribute('gen_ai.latency_ms', latency_ms)
                
                # Extract response metadata
                if hasattr(result, 'usage'):
                    span.set_attributes({
                        'gen_ai.usage.input_tokens': result.usage.prompt_tokens,
                        'gen_ai.usage.output_tokens': result.usage.completion_tokens,
                    })
                    
                    # GPT-5/o-series: Extract reasoning tokens from completion_tokens_details
                    if hasattr(result.usage, 'completion_tokens_details'):
                        details = result.usage.completion_tokens_details
                        if details and hasattr(details, 'reasoning_tokens'):
                            reasoning_tokens = details.reasoning_tokens or 0
                            span.set_attribute('gen_ai.usage.reasoning_tokens', reasoning_tokens)
                
                if hasattr(result, 'model'):
                    span.set_attribute('gen_ai.response.model', result.model)
                
                if hasattr(result, 'choices') and result.choices:
                    choice = result.choices[0]
                    if hasattr(choice, 'finish_reason'):
                        span.set_attribute('gen_ai.response.finish_reason', choice.finish_reason)
                    if hasattr(choice, 'message') and hasattr(choice.message, 'content'):
                        span.set_attribute('gen_ai.completion', _truncate(choice.message.content or '', 1000))
                
                return result
        
        openai.resources.chat.completions.Completions.create = traced_create
        
        # Instrument Responses API (GPT-5 visible reasoning)
        try:
            original_responses_create = openai.resources.responses.Responses.create
            
            @functools.wraps(original_responses_create)
            def traced_responses_create(self: Any, *args: Any, **kwargs: Any) -> Any:
                model = kwargs.get('model', 'unknown')
                
                with tracer.start_span("llm.call", kind=SpanKind.CLIENT) as span:
                    span.set_attributes({
                        'gen_ai.system': 'openai',
                        'gen_ai.request.model': model,
                    })
                    
                    if 'messages' in kwargs:
                        span.set_attribute('gen_ai.prompt', _format_messages(kwargs['messages']))
                    elif 'input' in kwargs:
                        # Capture input, handling list of messages or string
                        inp = kwargs['input']
                        if isinstance(inp, list):
                            span.set_attribute('gen_ai.prompt', _format_messages(inp))
                        else:
                            span.set_attribute('gen_ai.prompt', _truncate(str(inp), 1000))
                    
                    start_time = time.time()
                    result = original_responses_create(self, *args, **kwargs)
                    latency_ms = (time.time() - start_time) * 1000
                    
                    span.set_attribute('gen_ai.latency_ms', latency_ms)
                    
                    # Capture visible reasoning if available
                    if hasattr(result, 'reasoning_summary'):
                         span.set_attribute('gen_ai.usage.reasoning_text', result.reasoning_summary)
                    
                    if hasattr(result, 'output_text'):
                        span.set_attribute('gen_ai.completion', _truncate(result.output_text or '', 1000))

                    if hasattr(result, 'usage'):
                         span.set_attributes({
                            'gen_ai.usage.input_tokens': getattr(result.usage, 'input_tokens', 0),
                            'gen_ai.usage.output_tokens': getattr(result.usage, 'output_tokens', 0),
                         })

                    return result
            
            openai.resources.responses.Responses.create = traced_responses_create
            logger.debug("Instrumented OpenAI Responses API")
            
        except AttributeError:
             # Responses API not available in this SDK version
             pass

        # Also instrument async version
        try:
            original_acreate = openai.resources.chat.completions.AsyncCompletions.create
            
            @functools.wraps(original_acreate)
            async def traced_acreate(self: Any, *args: Any, **kwargs: Any) -> Any:
                model = kwargs.get('model', 'unknown')
                
                with tracer.start_span("llm.call", kind=SpanKind.CLIENT) as span:
                    span.set_attributes({
                        'gen_ai.system': 'openai',
                        'gen_ai.request.model': model,
                    })
                    
                    if 'messages' in kwargs:
                        span.set_attribute('gen_ai.prompt', _format_messages(kwargs['messages']))
                    
                    start_time = time.time()
                    result = await original_acreate(self, *args, **kwargs)
                    latency_ms = (time.time() - start_time) * 1000
                    
                    span.set_attribute('gen_ai.latency_ms', latency_ms)
                    
                    if hasattr(result, 'usage'):
                        span.set_attributes({
                            'gen_ai.usage.input_tokens': result.usage.prompt_tokens,
                            'gen_ai.usage.output_tokens': result.usage.completion_tokens,
                        })
                    
                    if hasattr(result, 'model'):
                        span.set_attribute('gen_ai.response.model', result.model)
                    
                    return result
            
            openai.resources.chat.completions.AsyncCompletions.create = traced_acreate
        except AttributeError:
            pass  # Async not available in this version
        
        openai._gate_instrumented = True  # type: ignore
        logger.debug("Instrumented OpenAI")
        return True
        
    except ImportError:
        return False
    except Exception as e:
        logger.debug(f"Failed to instrument OpenAI: {e}")
        return False


def _instrument_anthropic() -> bool:
    """
    Instrument Anthropic library.
    
    Wraps:
        - client.messages.create()
    
    Returns:
        True if instrumentation succeeded
    """
    try:
        import anthropic
        
        if hasattr(anthropic, '_gate_instrumented'):
            return True
        
        tracer = get_tracer("gate.anthropic")
        
        original_create = anthropic.resources.Messages.create
        
        @functools.wraps(original_create)
        def traced_create(self: Any, *args: Any, **kwargs: Any) -> Any:
            model = kwargs.get('model', 'unknown')
            
            with tracer.start_span("llm.call", kind=SpanKind.CLIENT) as span:
                span.set_attributes({
                    'gen_ai.system': 'anthropic',
                    'gen_ai.request.model': model,
                })
                
                if 'messages' in kwargs:
                    span.set_attribute('gen_ai.prompt', _format_messages(kwargs['messages']))
                
                start_time = time.time()
                result = original_create(self, *args, **kwargs)
                latency_ms = (time.time() - start_time) * 1000
                
                span.set_attribute('gen_ai.latency_ms', latency_ms)
                
                if hasattr(result, 'usage'):
                    span.set_attributes({
                        'gen_ai.usage.input_tokens': result.usage.input_tokens,
                        'gen_ai.usage.output_tokens': result.usage.output_tokens,
                    })
                
                if hasattr(result, 'model'):
                    span.set_attribute('gen_ai.response.model', result.model)
                
                if hasattr(result, 'stop_reason'):
                    span.set_attribute('gen_ai.response.finish_reason', result.stop_reason)
                
                return result
        
        anthropic.resources.Messages.create = traced_create
        anthropic._gate_instrumented = True  # type: ignore
        
        logger.debug("Instrumented Anthropic")
        return True
        
    except ImportError:
        return False
    except Exception as e:
        logger.debug(f"Failed to instrument Anthropic: {e}")
        return False


def _instrument_httpx() -> bool:
    """
    Instrument httpx library.
    
    Wraps:
        - client.request()
        - client.get/post/put/delete/etc.
    
    Returns:
        True if instrumentation succeeded
    """
    try:
        import httpx
        
        if hasattr(httpx, '_gate_instrumented'):
            return True
        
        tracer = get_tracer("gate.httpx")
        
        original_send = httpx.Client.send
        
        @functools.wraps(original_send)
        def traced_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
            span_name = f"HTTP {request.method}"
            
            with tracer.start_span(span_name, kind=SpanKind.CLIENT) as span:
                span.set_attributes({
                    'http.method': request.method,
                    'http.url': str(request.url),
                    'http.host': request.url.host,
                })
                
                start_time = time.time()
                response = original_send(self, request, *args, **kwargs)
                latency_ms = (time.time() - start_time) * 1000
                
                span.set_attributes({
                    'http.status_code': response.status_code,
                    'http.latency_ms': latency_ms,
                })
                
                return response
        
        httpx.Client.send = traced_send
        
        # Also instrument async client
        try:
            original_async_send = httpx.AsyncClient.send
            
            @functools.wraps(original_async_send)
            async def traced_async_send(self: Any, request: Any, *args: Any, **kwargs: Any) -> Any:
                span_name = f"HTTP {request.method}"
                
                with tracer.start_span(span_name, kind=SpanKind.CLIENT) as span:
                    span.set_attributes({
                        'http.method': request.method,
                        'http.url': str(request.url),
                        'http.host': request.url.host,
                    })
                    
                    start_time = time.time()
                    response = await original_async_send(self, request, *args, **kwargs)
                    latency_ms = (time.time() - start_time) * 1000
                    
                    span.set_attributes({
                        'http.status_code': response.status_code,
                        'http.latency_ms': latency_ms,
                    })
                    
                    return response
            
            httpx.AsyncClient.send = traced_async_send
        except AttributeError:
            pass
        
        httpx._gate_instrumented = True  # type: ignore
        logger.debug("Instrumented httpx")
        return True
        
    except ImportError:
        return False
    except Exception as e:
        logger.debug(f"Failed to instrument httpx: {e}")
        return False


def _instrument_requests() -> bool:
    """
    Instrument requests library.
    
    Wraps:
        - requests.Session.request()
    
    Returns:
        True if instrumentation succeeded
    """
    try:
        import requests
        
        if hasattr(requests, '_gate_instrumented'):
            return True
        
        tracer = get_tracer("gate.requests")
        
        original_request = requests.Session.request
        
        @functools.wraps(original_request)
        def traced_request(self: Any, method: str, url: str, *args: Any, **kwargs: Any) -> Any:
            span_name = f"HTTP {method}"
            
            with tracer.start_span(span_name, kind=SpanKind.CLIENT) as span:
                span.set_attributes({
                    'http.method': method,
                    'http.url': url,
                })
                
                start_time = time.time()
                response = original_request(self, method, url, *args, **kwargs)
                latency_ms = (time.time() - start_time) * 1000
                
                span.set_attributes({
                    'http.status_code': response.status_code,
                    'http.latency_ms': latency_ms,
                })
                
                return response
        
        requests.Session.request = traced_request
        requests._gate_instrumented = True  # type: ignore
        
        logger.debug("Instrumented requests")
        return True
        
    except ImportError:
        return False
    except Exception as e:
        logger.debug(f"Failed to instrument requests: {e}")
        return False


def _format_messages(messages: list) -> str:
    """Format chat messages for span attribute."""
    try:
        formatted = []
        for message in messages[:5]:  # Limit to first 5 messages
            if hasattr(message, 'content'):
                # Extract text from BaseMessage (e.g., LangChain)
                role = getattr(message, 'type', 'unknown')
                content = getattr(message, 'content', '')
            elif isinstance(message, dict):
                role = message.get('role', 'unknown')
                content = message.get('content', '')
            else:
                role = getattr(message, 'role', 'unknown')
                content = getattr(message, 'content', '')
            
            formatted.append(f"[{role}]: {_truncate(str(content), 200)}")
        
        if len(messages) > 5:
            formatted.append(f"... and {len(messages) - 5} more messages")
        
        return '\n'.join(formatted)
    except Exception:
        return str(messages)[:500]


def _truncate(s: str, max_len: int) -> str:
    """Truncate string to max length."""
    if len(s) <= max_len:
        return s
    return s[:max_len - 3] + "..."
