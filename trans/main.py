"""FL-GRT Trans (Transformation) Processor.

The Trans service is the asynchronous processing layer that:
1. Consumes trace data from the queue (Kafka or LocalFile)
2. Runs online evaluations and guardrail checks
3. Normalizes and enriches span data
4. Writes to the analytics storage (ClickHouse or SQLite)

Architecture:
    Reflect Collector → Queue → Trans Processor → Storage
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from pathlib import Path
from typing import Any

from .evaluators import EvaluatorPipeline
from .storage import StorageBackend, get_storage

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("trans")


class TransProcessor:
    """Main processor that consumes traces and writes to storage.
    
    The processor runs in a continuous loop, reading from the configured
    queue source and writing processed spans to the storage backend.
    
    Attributes:
        storage: The storage backend (SQLite/ClickHouse).
        evaluators: Pipeline of online evaluators.
        batch_size: Number of records to process per batch.
        poll_interval: Seconds between queue polls.
    """
    
    def __init__(
        self,
        storage: StorageBackend | None = None,
        batch_size: int = 100,
        poll_interval: float = 1.0,
    ) -> None:
        """Initialize the Trans processor.
        
        Args:
            storage: Storage backend instance. If None, uses get_storage().
            batch_size: Max records per processing batch.
            poll_interval: Seconds to wait between poll cycles.
        """
        self.storage = storage or get_storage()
        self.evaluators = EvaluatorPipeline()
        self.batch_size = batch_size
        self.poll_interval = poll_interval
        self._running = False
        self._shutdown_event = asyncio.Event()
        
        logger.info(f"Trans processor initialized (batch={batch_size})")
    
    async def start(self) -> None:
        """Start the processor loop.
        
        Runs until stop() is called or a shutdown signal is received.
        """
        self._running = True
        logger.info("Trans processor starting...")
        
        # Initialize storage
        await self.storage.initialize()
        
        # Register signal handlers (Unix only - Windows doesn't support add_signal_handler)
        if sys.platform != "win32":
            loop = asyncio.get_event_loop()
            for sig in (signal.SIGTERM, signal.SIGINT):
                loop.add_signal_handler(sig, self._handle_shutdown)
        
        await self._process_loop()
    
    def _handle_shutdown(self) -> None:
        """Handle shutdown signal gracefully."""
        logger.info("Shutdown signal received")
        self._running = False
        self._shutdown_event.set()
    
    async def stop(self) -> None:
        """Stop the processor gracefully."""
        self._running = False
        self._shutdown_event.set()
        await self.storage.close()
        logger.info("Trans processor stopped")
    
    async def _process_loop(self) -> None:
        """Main processing loop."""
        queue_type = os.getenv("REFLECT_QUEUE_TYPE", "file").lower()
        
        if queue_type == "kafka":
            await self._consume_kafka()
        else:
            await self._consume_file()
    
    async def _consume_file(self) -> None:
        """Consume from local JSONL file queue."""
        queue_path = Path(os.getenv("REFLECT_QUEUE_PATH", "./traces.jsonl"))
        processed_path = queue_path.with_suffix(".processed")
        
        logger.info(f"Consuming from file queue: {queue_path}")
        
        while self._running:
            try:
                if not queue_path.exists():
                    await asyncio.sleep(self.poll_interval)
                    continue
                
                # Read and process file
                records = []
                with queue_path.open("r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            try:
                                records.append(json.loads(line))
                            except json.JSONDecodeError:
                                logger.warning(f"Invalid JSON line: {line[:50]}...")
                
                if records:
                    await self._process_batch(records)
                    
                    # Move to processed file
                    with processed_path.open("a") as f:
                        for record in records:
                            f.write(json.dumps(record) + "\n")
                    
                    # Clear the queue file
                    queue_path.write_text("")
                    logger.info(f"Processed {len(records)} records from file queue")
                
                await asyncio.sleep(self.poll_interval)
                
            except Exception as e:
                logger.error(f"Error in file consumer: {e}", exc_info=True)
                await asyncio.sleep(self.poll_interval * 2)
    
    async def _consume_kafka(self) -> None:
        """Consume from Kafka topic."""
        try:
            from aiokafka import AIOKafkaConsumer
        except ImportError:
            logger.error("aiokafka required for Kafka consumption")
            return
        
        servers = os.getenv("REFLECT_KAFKA_SERVERS", "localhost:9092")
        topic = os.getenv("REFLECT_KAFKA_TOPIC", "agent-traces")
        group_id = os.getenv("TRANS_CONSUMER_GROUP", "trans-processor")
        
        consumer = AIOKafkaConsumer(
            topic,
            bootstrap_servers=servers,
            group_id=group_id,
            value_deserializer=lambda v: json.loads(v.decode("utf-8")),
            auto_offset_reset="earliest",
        )
        
        await consumer.start()
        logger.info(f"Kafka consumer started: {topic} @ {servers}")
        
        try:
            batch = []
            async for message in consumer:
                batch.append(message.value)
                
                if len(batch) >= self.batch_size:
                    await self._process_batch(batch)
                    batch = []
                    await consumer.commit()
                
                if not self._running:
                    break
            
            # Process remaining
            if batch:
                await self._process_batch(batch)
                await consumer.commit()
                
        finally:
            await consumer.stop()
            logger.info("Kafka consumer stopped")
    
    async def _process_batch(self, records: list[dict[str, Any]]) -> None:
        """Process a batch of span records.
        
        1. Run evaluators
        2. Enrich/normalize
        3. Write to storage
        
        Args:
            records: List of span dictionaries from queue.
        """
        start_time = time.perf_counter()
        
        # Run evaluators
        enriched = []
        for record in records:
            try:
                result = await self.evaluators.evaluate(record)
                record["_eval_results"] = result
                enriched.append(record)
            except Exception as e:
                logger.warning(f"Evaluator error: {e}")
                enriched.append(record)
        
        # Write to storage
        await self.storage.write_spans(enriched)
        
        duration = time.perf_counter() - start_time
        logger.debug(f"Batch processed: {len(records)} records in {duration:.3f}s")


async def main() -> None:
    """Entry point for running Trans as a standalone service."""
    processor = TransProcessor()
    
    try:
        await processor.start()
    except KeyboardInterrupt:
        pass
    finally:
        await processor.stop()


if __name__ == "__main__":
    asyncio.run(main())
