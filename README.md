# FL-GRT: Gate Runtime

[![PyPI version](https://badge.fury.io/py/FL-GRT.svg)](https://badge.fury.io/py/FL-GRT)
[![Python 3.8+](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/downloads/)
[![License: Apache-2.0](https://img.shields.io/badge/License-Apache%202.0-blue.svg)](https://opensource.org/licenses/Apache-2.0)
[![Documentation](https://readthedocs.org/projects/fl-grt/badge/?version=latest)](https://fl-grt.readthedocs.io/)

**Lightweight, Agent-Native Observability SDK for High-Performance AI Applications.**

FL-GRT provides zero-overhead instrumentation for AI agents with **non-blocking telemetry**, **automatic framework integration**, and **agent-native primitives** not found in traditional APM tools.

---

## Key Features

| Feature | Description |
|---------|-------------|
| **Non-Blocking Telemetry** | Agent's main thread never blocks for telemetry operations |
| **Zero GC at Runtime** | Memory allocated once at init, no garbage collection during execution |
| **Fail-Open Design** | Telemetry failures never affect agent behavior |
| **Agent-Native Primitives** | Chain-of-thought capture, cost/loop budgets, state snapshots, decision branching |
| **Automatic Integration** | Zero-code instrumentation via import hooks for popular frameworks |
| **Context Propagation** | Distributed tracing across HTTP boundaries with automatic header injection |

---

## Installation

```bash
pip install FL-GRT
```

### Optional Dependencies

For enhanced performance:

```bash
# Faster JSON serialization
pip install FL-GRT[orjson]

# Full performance stack
pip install FL-GRT[performance]
```

---

## Quick Start

### Basic Configuration

```python
import gate

# Configure the SDK
gate.configure(
    service_name="my-agent",
    endpoint="http://localhost:4318/v1/traces",  # OTLP endpoint
    sample_rate=1.0,
)

# Decorate functions for automatic tracing
@gate.observe
def my_agent_step():
    """Agent logic here."""
    pass
```

### Micro-Instrumentation

```python
import gate

@gate.observe
def process_request(user_input: str):
    # Track internal steps
    with gate.step("validate_input"):
        validated = validate(user_input)
    
    # Record metrics
    gate.track("token_count", count_tokens(validated))
    
    # Log events
    gate.event("processing_started", {"input_length": len(validated)})
    
    return generate_response(validated)
```

---

## Agent-Native Observability

FL-GRT provides primitives specifically designed for AI agents, unavailable in traditional APM tools like Datadog or New Relic.

### Chain-of-Thought Capture

```python
@gate.observe
def reasoning_agent(prompt: str):
    with gate.thought("Initial analysis") as t:
        t.add("User is asking about X")
        t.add("This requires approach Y")
        t.conclude("Will use strategy Z")
    
    return execute_strategy("Z")
```

### Cost & Loop Budgets (Guardrails)

```python
@gate.observe
def controlled_agent():
    # Prevent runaway costs and infinite loops
    with gate.guard(max_cost=0.50, max_iterations=10) as g:
        while not done:
            g.track_cost(get_llm_cost())
            g.increment()  # Track iterations
            result = llm_call()
```

### State Snapshots & Branching

```python
@gate.observe
def exploration_agent():
    # Capture state for debugging
    gate.snapshot("initial_state", {"memory": agent_memory})
    
    # Track decision trees
    with gate.branch("strategy_selection") as b:
        b.option("aggressive", score=0.7)
        b.option("conservative", score=0.3)
        b.select("aggressive")
```

---

## Automatic Framework Integration

Install import hooks for zero-code instrumentation:

```python
import gate

# Patch all supported frameworks (Google ADK, LangChain, etc.)
gate.instrument_all()

# Or selectively
gate.install_import_hook("google.adk")
```

---

## Distributed Tracing

Propagate context across services:

```python
import gate
import requests

# Automatic header injection
gate.instrument_http_clients()

# Or manual injection
headers = gate.get_injection_headers()
response = requests.get("http://downstream-service/api", headers=headers)

# Extract context from incoming requests
ctx = gate.extract_context(request.headers)
with gate.trace_from_headers(request.headers):
    handle_request()
```

---

## Environment Variables

| Variable | Description | Default |
|----------|-------------|---------|
| `GRT_ENDPOINT` | OTLP collector endpoint | `None` |
| `GRT_SERVICE_NAME` | Service name for traces | `unknown-service` |
| `GRT_SAMPLE_RATE` | Sampling rate (0.0-1.0) | `1.0` |
| `GRT_VERBOSE` | Enable verbose logging | `0` |
| `GRT_DISABLED` | Disable all telemetry | `0` |

---

## Design Principles

1. **Non-Blocking by Design**: Telemetry is offloaded to background threads; the agent's main loop is never blocked.
2. **Zero Allocation at Runtime**: All memory is pre-allocated at initialization to avoid GC pauses.
3. **Fail-Open Semantics**: If telemetry fails, the agent continues unaffected.
4. **Agent-Native, Not APM-Native**: Built from the ground up for AI agent patterns, not retrofitted from web services.

---

## Documentation

Full documentation is available at [fl-grt.readthedocs.io](https://fl-grt.readthedocs.io/).

- [API Reference](https://fl-grt.readthedocs.io/api.html)
- [Architecture](https://fl-grt.readthedocs.io/architecture.html)
- [Integration Guide](https://fl-grt.readthedocs.io/integration.html)
- [Scaling & Performance](https://fl-grt.readthedocs.io/scaling.html)

---

## Contributing

Contributions are welcome! Please see our [Contributing Guidelines](CONTRIBUTING.md) for details.

---

## License

Apache License 2.0. See [LICENSE](LICENSE) for details.

---

## Authors

**Jacob Lazar** — [jacob.la.research@gmail.com](mailto:jacob.la.research@gmail.com)

**Immanuel Mary Lazar**

GitHub: [github.com/Jacob-Lazar/GRT](https://github.com/Jacob-Lazar/GRT)
