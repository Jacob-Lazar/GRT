Integration Patterns
====================

.. meta::
   :description: Detailed integration patterns for FL-GRT: Native (Scenario A), Frameworks (Scenario B), and Low-Code (Scenario C).

FL-GRT supports three primary integration patterns to cover the entire landscape of AI agents, from custom Python code to low-code workflows.

Scenario A: Native Agents (Gate SDK)
------------------------------------

**Target**: Pure Python codebases, custom cognitive architectures, or existing "BaseAgent" implementations.

Mechanism
~~~~~~~~~
The SDK uses Python's ``sys.meta_path`` import hooks to intercept agent classes at runtime. This allows "Zero-Code" auto-instrumentation (Monkey Patching).

**How it works:**
1.  **Install Import Hook**: Register a finder that listens for your agent module.
2.  **Intercept Class Creation**: When the class is defined, wrap methods like ``run``, ``think``, and ``act``.
3.  **Inject Telemetry**: The wrappers emit spans to the Ring Buffer automatically.

.. code-block:: python

    # Manual implementation if not using auto-instrumentation
    from gate import observe, thought

    @observe("agent.run")
    def run_agent(query):
        with thought(reasoning="Initial plan"):
            # ...
            pass

Scenario B: Frameworks (OpenLLMetry)
------------------------------------

**Target**: LangChain, LlamaIndex, CrewAI, AutoGPT.

FL-GRT leverages the extensive ecosystem of **OpenLLMetry** (Traceloop) for broad compatibility, while adding our own "Cognitive" layer on top.

**Initialization:**

.. code-block:: python

    from gate import instrument_all

    # Instruments OpenAI, LangChain, LlamaIndex, etc.
    instrument_all(service_name="research-bot")

**LangGraph Support**:
FL-GRT (v0.1.0+) provides **Native LangGraph Instrumentation**. 
Legacy tools create fragmented traces (one per node). FL-GRT uses context propagation callbacks to stitch the entire graph execution into a single, unified Trace.

Scenario C: Low-Code (Webhooks)
-------------------------------

**Target**: n8n, Zapier, Make, Retool.

For platforms where you cannot install Python packages, Reflect serves as a "Remote Collector" via standard HTTP Webhooks.

**Webhook Payload**:

.. code-block:: json

    {
      "source": "n8n",
      "event": "llm.call",
      "model": "gpt-4",
      "input": "User query...",
      "output": "AI response...",
      "tokens": {
          "input": 150,
          "output": 45
      },
      "timestamp": "2024-01-20T10:00:00Z"
    }

**Planned Feature: n8n Node**
We are developing a dedicated ``@fl-grt/n8n-node`` to simplify this further, offering:
*   Auto-detection of OpenAI/Anthropic response formats.
*   One-click authentication.
*   Batch sending.
