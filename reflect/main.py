"""
FL-GRT 'Reflect' Collector Service

The central ingestion point for all agent telemetry.
"""
import gzip
import json
import logging
import time
from typing import Any, Callable, Optional

from fastapi import FastAPI, Request, BackgroundTasks
from fastapi.responses import JSONResponse

from .models import TracePayload, KeyValue
from .storage import init_db, insert_spans
from .auth import AuthMiddleware
from .metrics import MetricsMiddleware, metrics_endpoint, record_spans_ingested, record_db_write
from .webhook import router as webhook_router
from .queue import get_queue, TraceQueue
from .ratelimit import RateLimitMiddleware

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s"
)
logger = logging.getLogger("reflect")

# --- Application Setup ---

app = FastAPI(
    title="Reflect Collector",
    description="FL-GRT Telemetry Ingestion Service",
    version="1.1.0"
)

# --- Middleware Stack (order matters: first added = outermost) ---
# 1. Metrics (outermost - tracks all requests)
app.add_middleware(MetricsMiddleware)
# 2. Rate Limiting (reject excessive requests early)
app.add_middleware(RateLimitMiddleware)
# 3. Auth (reject unauthenticated before processing)
app.add_middleware(AuthMiddleware)


@app.middleware("http")
async def gzip_middleware(request: Request, call_next: Callable):
    """Handle gzip encoded request bodies."""
    if "gzip" in request.headers.get("Content-Encoding", ""):
        try:
            body = await request.body()
            decoded_body = gzip.decompress(body)
            
            # Replace the receive callable to return decompressed body
            async def receive():
                return {"type": "http.request", "body": decoded_body, "more_body": False}
            
            request._receive = receive
        except Exception as e:
            logger.error(f"Gzip decompression failed: {e}", exc_info=True)
            return JSONResponse(
                status_code=400,
                content={"error": f"Decompression failed: {str(e)}"}
            )
    
    return await call_next(request)


# --- Startup / Shutdown ---

# Global queue instance
_queue: Optional[TraceQueue] = None


@app.on_event("startup")
async def startup():
    global _queue
    logger.info("Reflect Collector starting up...")
    await init_db()
    logger.info("Database initialized.")
    _queue = get_queue()
    # Start Kafka queue if applicable
    if hasattr(_queue, "start"):
        await _queue.start()
    logger.info(f"Queue initialized: {type(_queue).__name__}")


@app.on_event("shutdown")
async def shutdown():
    """Gracefully close queue connections and flush pending messages."""
    global _queue
    logger.info("Reflect Collector shutting down...")
    if _queue:
        await _queue.close()
        logger.info("Queue closed.")


# --- Routers ---

app.include_router(webhook_router, prefix="/api/v1")


# --- Endpoints ---

@app.get("/health")
def health():
    """Liveness probe."""
    return {"status": "ok"}


@app.get("/metrics")
async def metrics():
    """Prometheus metrics endpoint."""
    return await metrics_endpoint()


def _extract_value(kv: KeyValue) -> Any:
    """Extract typed value from OTLP KeyValue structure."""
    v = kv.value
    if v.stringValue:
        return v.stringValue
    if v.intValue:
        return int(v.intValue)
    if v.doubleValue:
        return v.doubleValue
    if v.boolValue:
        return v.boolValue
    return str(v)


async def _process_trace_payload(payload: TracePayload):
    """Transform OTLP payload to flat records and persist."""
    flat_spans = []
    
    for resource_span in payload.resourceSpans:
        # Extract service name from resource attributes
        service_name = "unknown"
        for attr in resource_span.resource.attributes:
            if attr.key == "service.name" and attr.value.stringValue:
                service_name = attr.value.stringValue
                break
        
        for scope_span in resource_span.scopeSpans:
            for span in scope_span.spans:
                # Flatten attributes
                attrs = {kv.key: _extract_value(kv) for kv in span.attributes}
                
                # Flatten events
                events = []
                for evt in span.events:
                    evt_attrs = {kv.key: _extract_value(kv) for kv in evt.attributes}
                    events.append({
                        "name": evt.name,
                        "time": int(evt.timeUnixNano),
                        "attributes": evt_attrs
                    })
                
                start = int(span.startTimeUnixNano)
                end = int(span.endTimeUnixNano)
                
                record = {
                    "trace_id": span.traceId,
                    "span_id": span.spanId,
                    "parent_span_id": span.parentSpanId,
                    "name": span.name,
                    "kind": span.kind,
                    "start_time": start,
                    "end_time": end,
                    "duration_ns": end - start,
                    "status_code": span.status.code if span.status else 0,
                    "service_name": service_name,
                    "attributes_json": json.dumps(attrs),
                    "events_json": json.dumps(events),
                    "raw_payload": "",
                }
                flat_spans.append(record)
    
    if flat_spans:
        start_time = time.perf_counter()
        # Use queue for durable delivery, fallback to direct insert
        if _queue:
            await _queue.publish(flat_spans)
        else:
            await insert_spans(flat_spans)
        record_db_write(time.perf_counter() - start_time)
        record_spans_ingested(len(flat_spans), endpoint="otlp")


@app.post("/v1/traces")
async def ingest_traces(payload: TracePayload, background_tasks: BackgroundTasks):
    """
    OTLP/HTTP Trace Ingestion Endpoint.
    
    Accepts OpenTelemetry ExportTraceServiceRequest JSON payloads.
    """
    background_tasks.add_task(_process_trace_payload, payload)
    return {"status": "accepted", "items": len(payload.resourceSpans)}
