Data Model & Semantics
======================

.. meta::
   :description: FL-GRT Telemetry Data Model, Span Hierarchy, and Chain-of-Thought semantics.

FL-GRT extends the standard OpenTelemetry model with "Cognitive" semantics designed for AI Agents.

Trace Hierarchy
---------------

A typical Agent interaction creates a hierarchical trace structure that mirrors the cognitive process.

.. code-block:: text

    trace_id: a1b2c3...
    ├── span: agent.execute (Root)
    │   ├── attribute: input.query
    │   ├── attribute: output.result
    │   │
    │   ├── span: agent.thought (Iteration 1)
    │   │   ├── attribute: thought.reasoning
    │   │   ├── attribute: thought.decision
    │   │   └── attribute: thought.confidence (0.92)
    │   │
    │   ├── span: tool.call
    │   │   ├── attribute: tool.name="search_api"
    │   │   └── attribute: tool.duration_ms=450
    │   │
    │   └── span: llm.call (Generation)
    │       ├── attribute: llm.model="gpt-4"
    │       └── attribute: cost.usd=0.04

Semantic Attributes
-------------------

Chain of Thought (CoT)
~~~~~~~~~~~~~~~~~~~~~~
*   ``thought.reasoning`` (string): The internal monologue or reasoning step.
*   ``thought.decision`` (string): The specific action chosen (e.g., "call_search_tool").
*   ``thought.confidence`` (float): 0.0 to 1.0 confidence score.
*   ``thought.alternatives`` (list): Other options considered but rejected.

LLM Calls
~~~~~~~~~
*   ``llm.model``: e.g., ``gpt-4-turbo``.
*   ``llm.provider``: e.g., ``openai``, ``anthropic``.
*   ``tokens.input``: Prompt info.
*   ``tokens.output``: Completion info.
*   ``cost.usd``: Estimated cost of the call.

Guards & Safety
~~~~~~~~~~~~~~~
*   ``guard.type``: "cost_limit", "loop_limit".
*   ``guard.threshold``: The limit value.
*   ``guard.breach``: Boolean, true if limit exceeded.

Context Propagation
===================

FL-GRT uses **W3C Baggage** to propagate user and session context across distributed services (e.g., from a Next.js frontend to a Python Agent).

.. code-block:: text

    Header: baggage
    Value: user_id=alice,session_id=123,plan=pro

**Auto-Extraction**:
The Gate SDK automatically parses the ``baggage`` header and attaches these keys as attributes to every span in the trace. This enables you to filter traces by ``session_id`` in the dashboard.

**Cardinality Warning**:
To prevent "Cardinality Explosion" in the analytics database, the Collector employs an **Allowlist**. Only pre-configured keys (like ``session_id``, ``tenant_id``) are indexed. Random keys (like ``request_id``) are dropped or aggregated.
