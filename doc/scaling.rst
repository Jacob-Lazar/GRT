Scalability & Reliability
=========================

.. meta::
   :description: Deployment architecture, Kafka buffering, and reliability engineering for FL-GRT.

The FL-GRT architecture is designed for "Day 2" operations, handling high-throughput telemetry without data loss.

Ingestion Pipeline
------------------

.. mermaid::

    graph LR
        Agents((Agents)) -- HTTP --> LB[Load Balancer]
        LB --> Collect[Reflect Service]
        Collect -- Async --> Kafka[(Kafka / Redpanda)]
        Kafka --> Process[Trans Service]
        Process --> ClickHouse[(ClickHouse)]

Reflect (Collector)
~~~~~~~~~~~~~~~~~~~
*   **Stateless**: Can scale horizontally to thousands of instances.
*   **Circuit Breaking**: Automatically rejects traffic if Kafka is backlogged.
*   **Validation**: Enforces schema at the edge.

Message Queue (Kafka)
~~~~~~~~~~~~~~~~~~~~~
The buffer between ingestion and processing. Use **Redpanda** for a lighter operational footprint.
*   **Topic**: ``agent-traces``
*   **Partitioning**: Keyed by ``trace_id`` to ensure all spans for a transaction land on the same consumer.
*   **Retention**: 7 days (allows replay of processing logic).

Storage Strategy
----------------

1. **ClickHouse (Traces)**
   *   Columnar storage for fast OLAP queries.
   *   Partitioned by ``toYYYYMM(start_time)``.
   *   **TTL**: Hot storage (SSD) for 30 days, cold storage (S3) for 1 year.

2. **Redis (Topology)**
   *   Stores the latest state of "Active Agents".
   *   Used for the "Live View" dashboard.

Reliability Guarantees
----------------------

*   **Fail-Open**: If the collector is down, the SDK drops traces, never crashes the agent.
*   **At-Least-Once**: Kafka consumers commit offsets only after successful DB write.
*   **Backpressure**: The SDK Ring Buffer drops spans when the export rate cannot keep up.

Evaluations (Evals)
-------------------

Evaluations are "Unit Tests for Agents" that run on production data.

Online Evals
~~~~~~~~~~~~
Run inside the **Trans** processor in real-time.
*   **Latency Checks**: "Did response take > 5s?"
*   **Regex Guards**: "Did output contain PII pattern?"

Offline Evals
~~~~~~~~~~~~~
Run on a schedule against stored traces.
*   **LLM-as-a-Judge**: Re-read the conversation and score "Helpfulness" (1-5).
*   **RAG Quality**: Check relevance of retrieved docs to the user query.
