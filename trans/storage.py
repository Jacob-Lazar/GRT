"""Storage backends for Trans Processor.

Provides abstraction over different analytics storage systems.
"""

from __future__ import annotations

import json
import logging
import os
from abc import ABC, abstractmethod
from typing import Any

logger = logging.getLogger("trans.storage")


class StorageBackend(ABC):
    """Abstract base class defining the storage interface."""
    
    @abstractmethod
    async def initialize(self) -> None:
        """Initialize the storage backend (create tables, etc)."""
        ...
    
    @abstractmethod
    async def write_spans(self, spans: list[dict[str, Any]]) -> None:
        """Write a batch of spans to storage."""
        ...
    
    @abstractmethod
    async def close(self) -> None:
        """Close storage connections."""
        ...


class SQLiteStorage(StorageBackend):
    """SQLite-based storage for development and small deployments.
    
    Writes processed spans to a SQLite database with evaluation results.
    """
    
    def __init__(self, db_path: str = "trans_traces.db") -> None:
        self.db_path = db_path
        self._connection = None
    
    async def initialize(self) -> None:
        """Create tables if they don't exist."""
        import aiosqlite
        
        self._connection = await aiosqlite.connect(self.db_path)
        
        await self._connection.executescript("""
            CREATE TABLE IF NOT EXISTS processed_spans (
                trace_id TEXT NOT NULL,
                span_id TEXT NOT NULL PRIMARY KEY,
                parent_span_id TEXT,
                name TEXT NOT NULL,
                kind INTEGER,
                start_time INTEGER,
                end_time INTEGER,
                duration_ns INTEGER,
                status_code INTEGER,
                service_name TEXT,
                attributes_json TEXT,
                events_json TEXT,
                eval_results_json TEXT,
                processed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            
            CREATE INDEX IF NOT EXISTS idx_processed_trace_id ON processed_spans(trace_id);
            CREATE INDEX IF NOT EXISTS idx_processed_start_time ON processed_spans(start_time);
            CREATE INDEX IF NOT EXISTS idx_processed_service ON processed_spans(service_name);
        """)
        await self._connection.commit()
        logger.info(f"SQLite storage initialized: {self.db_path}")
    
    async def write_spans(self, spans: list[dict[str, Any]]) -> None:
        """Write spans with evaluation results."""
        if not spans or not self._connection:
            return
        
        query = """
            INSERT OR REPLACE INTO processed_spans (
                trace_id, span_id, parent_span_id, name, kind,
                start_time, end_time, duration_ns, status_code,
                service_name, attributes_json, events_json, eval_results_json
            ) VALUES (
                :trace_id, :span_id, :parent_span_id, :name, :kind,
                :start_time, :end_time, :duration_ns, :status_code,
                :service_name, :attributes_json, :events_json, :eval_results_json
            )
        """
        
        records = []
        for span in spans:
            record = {
                "trace_id": span.get("trace_id", ""),
                "span_id": span.get("span_id", ""),
                "parent_span_id": span.get("parent_span_id"),
                "name": span.get("name", ""),
                "kind": span.get("kind", 0),
                "start_time": span.get("start_time", 0),
                "end_time": span.get("end_time", 0),
                "duration_ns": span.get("duration_ns", 0),
                "status_code": span.get("status_code", 0),
                "service_name": span.get("service_name", ""),
                "attributes_json": span.get("attributes_json", "{}"),
                "events_json": span.get("events_json", "[]"),
                "eval_results_json": json.dumps(span.get("_eval_results", {})),
            }
            records.append(record)
        
        await self._connection.executemany(query, records)
        await self._connection.commit()
        logger.debug(f"Wrote {len(records)} spans to SQLite")
    
    async def close(self) -> None:
        """Close the database connection."""
        if self._connection:
            await self._connection.close()
            logger.info("SQLite storage closed")


class ClickHouseStorage(StorageBackend):
    """ClickHouse-based storage for production scale.
    
    Writes to a ClickHouse cluster using async HTTP interface.
    Requires `httpx` for async HTTP requests.
    """
    
    def __init__(
        self,
        host: str = "localhost",
        port: int = 8123,
        database: str = "default",
        table: str = "processed_spans",
    ) -> None:
        self.base_url = f"http://{host}:{port}"
        self.database = database
        self.table = table
        self._client = None
    
    async def initialize(self) -> None:
        """Create ClickHouse table if it doesn't exist."""
        try:
            import httpx
        except ImportError:
            logger.error("httpx required for ClickHouse storage")
            return
        
        self._client = httpx.AsyncClient(base_url=self.base_url)
        
        create_table = f"""
            CREATE TABLE IF NOT EXISTS {self.database}.{self.table} (
                trace_id String,
                span_id String,
                parent_span_id Nullable(String),
                name String,
                kind UInt8,
                start_time UInt64,
                end_time UInt64,
                duration_ns UInt64,
                status_code UInt8,
                service_name LowCardinality(String),
                attributes String,
                events String,
                eval_results String,
                processed_at DateTime DEFAULT now()
            )
            ENGINE = MergeTree()
            PARTITION BY toYYYYMM(processed_at)
            ORDER BY (service_name, trace_id, start_time)
            TTL processed_at + INTERVAL 90 DAY
        """
        
        response = await self._client.post("/", content=create_table)
        if response.status_code == 200:
            logger.info(f"ClickHouse table initialized: {self.database}.{self.table}")
        else:
            logger.error(f"ClickHouse table creation failed: {response.text}")
    
    async def write_spans(self, spans: list[dict[str, Any]]) -> None:
        """Write spans to ClickHouse using JSONEachRow format."""
        if not spans or not self._client:
            return
        
        rows = []
        for span in spans:
            row = {
                "trace_id": span.get("trace_id", ""),
                "span_id": span.get("span_id", ""),
                "parent_span_id": span.get("parent_span_id"),
                "name": span.get("name", ""),
                "kind": span.get("kind", 0),
                "start_time": span.get("start_time", 0),
                "end_time": span.get("end_time", 0),
                "duration_ns": span.get("duration_ns", 0),
                "status_code": span.get("status_code", 0),
                "service_name": span.get("service_name", ""),
                "attributes": span.get("attributes_json", "{}"),
                "events": span.get("events_json", "[]"),
                "eval_results": json.dumps(span.get("_eval_results", {})),
            }
            rows.append(json.dumps(row))
        
        body = "\n".join(rows)
        query = f"INSERT INTO {self.database}.{self.table} FORMAT JSONEachRow"
        
        response = await self._client.post(
            "/",
            params={"query": query},
            content=body,
            headers={"Content-Type": "application/json"},
        )
        
        if response.status_code == 200:
            logger.debug(f"Wrote {len(spans)} spans to ClickHouse")
        else:
            logger.error(f"ClickHouse write failed: {response.text}")
    
    async def close(self) -> None:
        """Close the HTTP client."""
        if self._client:
            await self._client.aclose()
            logger.info("ClickHouse storage closed")


def get_storage() -> StorageBackend:
    """Factory function to get configured storage backend.
    
    Uses TRANS_STORAGE_TYPE environment variable:
    - `sqlite` (default): Local SQLite database
    - `clickhouse`: ClickHouse cluster
    
    Returns:
        Configured storage backend instance.
    """
    storage_type = os.getenv("TRANS_STORAGE_TYPE", "sqlite").lower()
    
    if storage_type == "clickhouse":
        return ClickHouseStorage(
            host=os.getenv("CLICKHOUSE_HOST", "localhost"),
            port=int(os.getenv("CLICKHOUSE_PORT", "8123")),
            database=os.getenv("CLICKHOUSE_DATABASE", "default"),
        )
    
    # Default: SQLite
    return SQLiteStorage(
        db_path=os.getenv("TRANS_DB_PATH", "trans_traces.db")
    )
