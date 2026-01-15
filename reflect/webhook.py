"""
Webhook Endpoint for Low-Code Platforms (Scenario C).

Accepts simplified JSON payloads from n8n, Zapier, Make, etc.
and normalizes them to the internal span format.
"""
import json
import uuid
import logging
from datetime import datetime, timezone
from typing import Optional
from pydantic import BaseModel, Field
from fastapi import APIRouter, BackgroundTasks

from .storage import insert_spans

logger = logging.getLogger("reflect.webhook")

router = APIRouter(tags=["webhook"])


class TokenUsage(BaseModel):
    input: int = 0
    output: int = 0


class WebhookPayload(BaseModel):
    """
    Simplified payload schema for low-code platforms.
    
    Required: source, workflow_id, event
    Optional: Everything else
    """
    source: str = Field(..., description="Platform identifier (e.g., 'n8n', 'zapier')")
    workflow_id: str = Field(..., description="Unique workflow/automation ID")
    execution_id: Optional[str] = Field(None, description="Execution run ID")
    event: str = Field(..., description="Event type: llm.call, tool.call, workflow.start, etc.")
    
    # Optional fields
    timestamp: Optional[str] = None
    model: Optional[str] = None
    input: Optional[str] = None
    output: Optional[str] = None
    tokens: Optional[TokenUsage] = None
    latency_ms: Optional[int] = None
    error: Optional[str] = None
    metadata: Optional[dict] = None


def _generate_ids() -> tuple[str, str]:
    """Generate trace_id and span_id."""
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    return trace_id, span_id


def _parse_timestamp(ts: Optional[str]) -> int:
    """Parse ISO timestamp to nanoseconds, or use current time."""
    if ts:
        try:
            dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            return int(dt.timestamp() * 1_000_000_000)
        except ValueError:
            pass
    return int(datetime.now(timezone.utc).timestamp() * 1_000_000_000)


async def _process_webhook(payload: WebhookPayload):
    """Transform webhook payload to span record and persist."""
    trace_id, span_id = _generate_ids()
    start_time = _parse_timestamp(payload.timestamp)
    duration_ns = (payload.latency_ms or 0) * 1_000_000
    
    # Build attributes
    attrs = {
        "webhook.source": payload.source,
        "webhook.workflow_id": payload.workflow_id,
    }
    if payload.execution_id:
        attrs["webhook.execution_id"] = payload.execution_id
    if payload.model:
        attrs["llm.model"] = payload.model
    if payload.input:
        attrs["input"] = payload.input[:10000]  # Truncate
    if payload.output:
        attrs["output"] = payload.output[:10000]
    if payload.tokens:
        attrs["tokens.input"] = payload.tokens.input
        attrs["tokens.output"] = payload.tokens.output
    if payload.error:
        attrs["error.message"] = payload.error
    if payload.metadata:
        for k, v in payload.metadata.items():
            attrs[f"metadata.{k}"] = str(v)
    
    record = {
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": None,
        "name": payload.event,
        "kind": 1,  # SPAN_KIND_INTERNAL
        "start_time": start_time,
        "end_time": start_time + duration_ns,
        "duration_ns": duration_ns,
        "status_code": 2 if payload.error else 1,  # ERROR or OK
        "service_name": f"{payload.source}:{payload.workflow_id}",
        "attributes_json": json.dumps(attrs),
        "events_json": "[]",
        "raw_payload": "",
    }
    
    await insert_spans([record])
    logger.info(f"Webhook span ingested: {payload.event} from {payload.source}")


@router.post("/webhook")
async def receive_webhook(payload: WebhookPayload, background_tasks: BackgroundTasks):
    """
    Receive trace data from low-code platforms.
    
    This endpoint accepts a simplified JSON payload and converts it
    to an internal span format for storage.
    """
    background_tasks.add_task(_process_webhook, payload)
    return {
        "status": "accepted",
        "source": payload.source,
        "event": payload.event,
    }
