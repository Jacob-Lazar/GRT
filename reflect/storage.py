"""
Async SQLite Storage Engine
"""
import aiosqlite
import json
import logging
import os
from datetime import datetime
from typing import List, Optional
from .models import Span

logger = logging.getLogger("collector.storage")

DB_PATH = "traces.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS traces (
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
    raw_payload TEXT,
    ingested_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_trace_id ON traces(trace_id);
CREATE INDEX IF NOT EXISTS idx_start_time ON traces(start_time);
"""

async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    logger.info(f"Database initialized at {DB_PATH}")

async def insert_spans(spans: List[dict]):
    """Batch insert spans."""
    if not spans:
        return

    query = """
    INSERT INTO traces (
        trace_id, span_id, parent_span_id, name, kind, 
        start_time, end_time, duration_ns, status_code, 
        service_name, attributes_json, events_json, raw_payload
    ) VALUES (
        :trace_id, :span_id, :parent_span_id, :name, :kind,
        :start_time, :end_time, :duration_ns, :status_code,
        :service_name, :attributes_json, :events_json, :raw_payload
    )
    ON CONFLICT(span_id) DO NOTHING;
    """
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executemany(query, spans)
        await db.commit()
    
    logger.info(f"Ingested {len(spans)} spans")
