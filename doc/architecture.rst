FL-GRT System Architecture
==========================

.. meta::
   :description: Comprehensive architecture of the FL-GRT (Gate) Observability Platform.

Executive Summary
-----------------

**FL-GRT** (Gateway for Research Telemetry) is a high-performance, agent-native observability framework designed to illuminate the "black box" of AI Agents. Unlike generic tracing tools, FL-GRT treats **Chain-of-Thought (CoT)**, **State Changes**, and **Decision Branching** as first-class citizens.

Core Philosophy
~~~~~~~~~~~~~~~

1.  **Agent-Native**: Primitives designed for cognitive architectures (``thought``, ``step``, ``guard``), not just HTTP requests.
2.  **Zero-Overhead**: Non-blocking **Ring Buffer** architecture ensures observability never slows down the agent.
3.  **Open Standards**: 100% compliant with **OpenTelemetry (OTel)** OTLP/HTTP protocol.
4.  **Framework Agnostic**: Works with raw Python, LangChain, PydanticAI, or any other stack.

System Architecture
-------------------

The platform consists of three primary components: **Gate** (SDK), **Reflect** (Collector), and **Trans** (Processor).

.. mermaid::

    graph LR
        classDef sdk fill:#e1f5fe,stroke:#01579b,stroke-width:2px;
        classDef svc fill:#e8f5e9,stroke:#2e7d32,stroke-width:2px;
        classDef db fill:#fff3e0,stroke:#ef6c00,stroke-width:2px;

        subgraph "Application Layer"
            Agent[Agent Logic]:::sdk
            Gate[Gate SDK]:::sdk
            Agent --> Gate
        end

        subgraph "Transport Layer"
            Reflect[Reflect Collector]:::svc
            Gate -- OTLP/HTTP --> Reflect
        end

        subgraph "Processing Layer"
            Kafka{Message Queue}:::db
            Trans[Trans Service]:::svc
            Reflect --> Kafka
            Kafka --> Trans
        end

        subgraph "Storage & View"
            DB[(ClickHouse/SQLite)]:::db
            Dash[Dashboard]:::svc
            Trans --> DB
            DB --> Dash
        end

1. Gate (SDK)
~~~~~~~~~~~~~
The **Gate SDK** (``fl_grt``) runs in-process with the agent. It is responsible for capturing telemetry with nanosecond-level efficiency (see :doc:`internals` for the deep dive on the Ring Buffer architecture).

**Key Primitives:**

*   ``@observe``: Root entry point for agent execution.
*   ``thought``: Captures internal reasoning chains and confidence scores.
*   ``guard``: Defines safety boundaries (cost, loops).
*   ``snapshot``: Captures full memory/state at specific points.

2. Reflect (Collector)
~~~~~~~~~~~~~~~~~~~~~~
**Reflect** is the high-throughput ingestion service. It acts as the "Gateway" for all telemetry data.

*   **Protocol**: Accepts OTLP/HTTP JSON payloads.
*   **Responsibility**: Validation, authentication, and fast persistence to the durability layer (Kafka or direct DB in dev mode).
*   **Scalability**: Stateless design allows horizontal scaling behind a load balancer.

3. Trans (Processor)
~~~~~~~~~~~~~~~~~~~~
**Trans** (Transformation Service) is the asynchronous brain of the platform.

*   **Input**: Consumes raw trace data from Reflect (via Kafka).
*   **Eval**: Runs online evaluations and guardrail checks on the stream.
*   **Transform**: Normalizes data into a query-optimized schema.
*   **Output**: Writes structured traces and metrics to the analytic storage (ClickHouse).

Integration Scenarios
---------------------

FL-GRT supports three primary integration patterns:

Scenario A: Native Integration (Zero-Code)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Target: **Pure Python Agents** or Neosapients BaseAgent.

The SDK uses Python import hooks to auto-instrument agent classes.
*   **Mechanism**: Monkey-patching at import time.
*   **Tracing**: Automatically captures ``run``, ``think``, ``act`` methods.
*   **Usage**: Simply run ``gate-run my_agent.py``.

Scenario B: Framework Integration (OpenLLMetry)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Target: **LangChain, LlamaIndex, CrewAI**.

Integrates with OpenLLMetry to capture framework-specific events, while enriching them with FL-GRT's agent-native semantics.
*   **Mechanism**: Connects to framework callbacks/hooks.
*   **Usage**: ``gate.instrument_all()`` at startup.

Scenario C: Low-Code Integration (Webhooks)
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Target: **n8n, Zapier, Make**.

Reflect exposes a ``/webhook`` endpoint that accepts simplified JSON payloads from low-code platforms and converts them into OTLP traces.

Data Flow & Latency
-------------------

.. list-table:: System Latency Budget
   :widths: 25 25 50
   :header-rows: 1

   * - Hop
     - Latency
     - Constraint
   * - **Agent → Gate**
     - < 0.1ms
     - Zero-blocking Ring Buffer write.
   * - **Gate → Reflect**
     - 5-20ms
     - Async background batch export.
   * - **Reflect → Trans**
     - < 10ms
     - Kafka produce/consume.
   * - **Total Visibility**
     - < 1s
     - Time from agent action to matching dashboard query.

Detailed implementation of the **Gate SDK** internals, including the Ring Buffer and GIL management, is available in :doc:`internals`.
