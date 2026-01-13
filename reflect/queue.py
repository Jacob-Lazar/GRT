"""Queue abstraction for durable trace delivery.

This module provides a pluggable queue interface for decoupling trace ingestion
from storage. In production, traces flow through Kafka for durability and
horizontal scaling. In development, a local file-based queue is available.

Architecture:
    Gate SDK → Reflect Collector → TraceQueue → Trans Processor → Storage
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, runtime_checkable

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger("reflect.queue")


@runtime_checkable
class TraceQueue(Protocol):
    """Protocol defining the queue interface for trace delivery.
    
    Implementations must provide async `publish` and `close` methods.
    The queue is responsible for durable delivery of span records to
    downstream processors.
    
    Example:
        ```python
        queue: TraceQueue = KafkaTraceQueue(bootstrap_servers="kafka:9092")
        await queue.publish([{"trace_id": "abc", "span_id": "123", ...}])
        await queue.close()
        ```
    """
    
    async def publish(self, spans: list[dict[str, Any]]) -> None:
        """Publish a batch of span records to the queue.
        
        Args:
            spans: List of flattened span dictionaries ready for storage.
            
        Raises:
            QueuePublishError: If publishing fails after retries.
        """
        ...
    
    async def close(self) -> None:
        """Gracefully close the queue connection.
        
        Ensures all pending messages are flushed before shutdown.
        """
        ...


class QueuePublishError(Exception):
    """Exception raised when queue publishing fails.
    
    Attributes:
        spans_count: Number of spans that failed to publish.
        reason: Description of the failure.
    """
    
    def __init__(self, spans_count: int, reason: str) -> None:
        self.spans_count = spans_count
        self.reason = reason
        super().__init__(f"Failed to publish {spans_count} spans: {reason}")


class LocalFileQueue:
    """File-based queue for local development.
    
    Appends span records as JSON Lines to a local file. Useful for
    development and testing without requiring Kafka infrastructure.
    
    The Trans processor can read from this file in a separate process.
    
    Attributes:
        path: Path to the JSONL output file.
    
    Example:
        ```python
        queue = LocalFileQueue(path="./traces.jsonl")
        await queue.publish([{"trace_id": "abc", ...}])
        ```
    """
    
    def __init__(self, path: str | Path = "./traces.jsonl") -> None:
        """Initialize the file queue.
        
        Args:
            path: Path to the output file. Created if it doesn't exist.
        """
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        logger.info(f"LocalFileQueue initialized: {self.path}")
    
    async def publish(self, spans: list[dict[str, Any]]) -> None:
        """Append spans to the JSONL file.
        
        Each span is written as a single JSON line with a timestamp.
        
        Args:
            spans: List of span dictionaries.
        """
        timestamp = datetime.utcnow().isoformat()
        with self.path.open("a") as f:
            for span in spans:
                record = {"_ingested_at": timestamp, **span}
                f.write(json.dumps(record) + "\n")
        logger.debug(f"Published {len(spans)} spans to {self.path}")
    
    async def close(self) -> None:
        """No-op for file queue (file handles are not kept open)."""
        pass


class KafkaTraceQueue:
    """Kafka-based queue for production deployments.
    
    Publishes span records to a Kafka topic for durable, scalable delivery.
    The Trans processor consumes from this topic asynchronously.
    
    Requires `aiokafka` to be installed.
    
    Attributes:
        topic: Kafka topic name.
        bootstrap_servers: Kafka broker addresses.
    
    Example:
        ```python
        queue = KafkaTraceQueue(
            bootstrap_servers="kafka:9092",
            topic="agent-traces"
        )
        await queue.start()
        await queue.publish([{"trace_id": "abc", ...}])
        await queue.close()
        ```
    """
    
    def __init__(
        self,
        bootstrap_servers: str = "localhost:9092",
        topic: str = "agent-traces",
    ) -> None:
        """Initialize the Kafka queue.
        
        Args:
            bootstrap_servers: Comma-separated Kafka broker addresses.
            topic: Kafka topic to publish to.
        """
        self.bootstrap_servers = bootstrap_servers
        self.topic = topic
        self._producer = None
        logger.info(f"KafkaTraceQueue configured: {bootstrap_servers} / {topic}")
    
    async def start(self) -> None:
        """Start the Kafka producer.
        
        Must be called before publishing. Establishes connection to brokers.
        
        Raises:
            ImportError: If aiokafka is not installed.
        """
        try:
            from aiokafka import AIOKafkaProducer
        except ImportError as e:
            raise ImportError(
                "aiokafka is required for Kafka queue. "
                "Install it with: pip install aiokafka"
            ) from e
        
        self._producer = AIOKafkaProducer(
            bootstrap_servers=self.bootstrap_servers,
            value_serializer=lambda v: json.dumps(v).encode("utf-8"),
        )
        await self._producer.start()
        logger.info("Kafka producer started")
    
    async def publish(self, spans: list[dict[str, Any]]) -> None:
        """Publish spans to Kafka topic.
        
        Each span is sent as an individual message. The trace_id is used
        as the partition key to ensure all spans for a trace land on the
        same partition.
        
        Args:
            spans: List of span dictionaries.
            
        Raises:
            QueuePublishError: If producer is not started or send fails.
        """
        if self._producer is None:
            raise QueuePublishError(len(spans), "Producer not started. Call start() first.")
        
        for span in spans:
            key = span.get("trace_id", "").encode("utf-8")
            await self._producer.send_and_wait(self.topic, value=span, key=key)
        
        logger.debug(f"Published {len(spans)} spans to Kafka topic {self.topic}")
    
    async def close(self) -> None:
        """Stop the Kafka producer and flush pending messages."""
        if self._producer:
            await self._producer.stop()
            logger.info("Kafka producer stopped")


def get_queue() -> TraceQueue:
    """Factory function to get the configured queue implementation.
    
    Uses environment variable `REFLECT_QUEUE_TYPE` to determine which
    queue to use:
    - `kafka`: KafkaTraceQueue (requires REFLECT_KAFKA_SERVERS)
    - `file`: LocalFileQueue (default for development)
    
    Returns:
        A TraceQueue implementation based on configuration.
    
    Example:
        ```python
        queue = get_queue()
        await queue.publish(spans)
        ```
    """
    queue_type = os.getenv("REFLECT_QUEUE_TYPE", "file").lower()
    
    if queue_type == "kafka":
        servers = os.getenv("REFLECT_KAFKA_SERVERS", "localhost:9092")
        topic = os.getenv("REFLECT_KAFKA_TOPIC", "agent-traces")
        return KafkaTraceQueue(bootstrap_servers=servers, topic=topic)
    
    # Default: file-based queue
    path = os.getenv("REFLECT_QUEUE_PATH", "./traces.jsonl")
    return LocalFileQueue(path=path)
