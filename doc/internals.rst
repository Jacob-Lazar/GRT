SDK Internals & Performance
===========================

.. meta::
   :description: Deep dive into FL-GRT SDK internals: Ring Buffer, GIL, and Non-blocking architecture.

The "Golden Rule": Non-Blocking
-------------------------------

**Requirement**: The SDK must introduce **zero blocking latency** to the instrumented agent's critical execution path.

**Technical Implementation**:

*   All trace exports execute in dedicated background threads (Daemon mode).
*   Agent execution continues immediately after ``span.end()`` without waiting for I/O.
*   SDK employs a pre-allocated **Lock-Free Ring Buffer** providing O(1) write operations.

The Ring Buffer
---------------

The Ring Buffer is the core component that enables zero-overhead telemetry.

Why Ring Buffer?
~~~~~~~~~~~~~~~~

To transfer span data from the agent's main thread to the exporter thread without blocking, we need a data structure that satisfies:

1.  **Non-Blocking Writes**: Main thread never waits.
2.  **Thread Safety**: Safe concurrency between Main (Writer) and Exporter (Reader).
3.  **Bounded Memory**: Fixed footprint, no dynamic allocation during execution.
4.  **Graceful Overflow**: Drop-newest policy under extreme load.

Memory Allocation & The GIL
~~~~~~~~~~~~~~~~~~~~~~~~~~~

Dynamic memory allocation in Python involves the **GIL (Global Interpreter Lock)** and potential Garbage Collector (GC) pauses.
To avoid this, FL-GRT allocates the buffer **once** at initialization.

.. code-block:: python

    class RingBuffer:
        def __init__(self, capacity=4096):
            # Pre-allocate references/slots
            self._buffer = [None] * capacity
            self._head = 0  # Write index
            self._tail = 0  # Read index
            self._capacity = capacity

Threading Architecture
----------------------

.. mermaid::

    sequenceDiagram
        participant Agent as Main Thread
        participant Buffer as Ring Buffer
        participant Exporter as Background Thread

        Note over Agent: Application Logic
        Agent->>Agent: Run heavy inference
        Agent->>Buffer: Write Span (O(1))
        Note right of Buffer: No Lock Wait
        Agent->>Agent: Continue execution...

        Note over Exporter: Independent Daemon
        loop Every 100ms
            Exporter->>Buffer: Check for data?
            Exporter->>Buffer: Read Batch (up to 512)
            Exporter->>Exporter: Serialize OTLP
            Exporter->>Collector: HTTP POST
        end

Benchmarks
----------

| Metric | FL-GRT SDK | Standard OTel SDK |
| :--- | :--- | :--- |
| **Span Overhead** | < 0.3ms | ~1.5ms |
| **Memory per Span** | ~1.5KB | ~4KB |
| **Blocking Time** | **0ms** | Variable (Lock contention) |
